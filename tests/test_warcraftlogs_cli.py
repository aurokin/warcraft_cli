from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner
from warcraftlogs_cli.client import (
    EncounterRankingsOptions,
    RETAIL_PROFILE,
    WarcraftLogsClient,
    WarcraftLogsClientError,
    _encounter_rankings_request,
    load_warcraftlogs_auth_config,
)
from warcraftlogs_cli.main import app as warcraftlogs_app

runner = CliRunner()


class _FakeWarcraftLogsClient:
    def __init__(self) -> None:
        self.closed = False
        self._guild_ttl = 300

    def close(self) -> None:
        self.closed = True

    def authorization_code_url(self, *, redirect_uri: str, state: str) -> str:
        return f"https://www.warcraftlogs.com/oauth/authorize?redirect_uri={redirect_uri}&state={state}&response_type=code"

    def pkce_code_url(self, *, redirect_uri: str, state: str, code_challenge: str) -> str:
        return (
            "https://www.warcraftlogs.com/oauth/authorize"
            f"?redirect_uri={redirect_uri}&state={state}&code_challenge={code_challenge}&response_type=code"
        )

    def exchange_authorization_code(self, *, code: str, redirect_uri: str) -> dict[str, object]:
        assert code == "code-123"
        assert redirect_uri == "http://127.0.0.1:8787/callback"
        return {
            "access_token": "user-token",
            "refresh_token": "refresh-token",
            "token_type": "Bearer",
            "scope": "reports",
            "expires_in": 3600,
        }

    def exchange_pkce_code(self, *, code: str, redirect_uri: str, code_verifier: str) -> dict[str, object]:
        assert code == "code-456"
        assert redirect_uri == "http://127.0.0.1:8787/callback"
        assert code_verifier == "verifier-123"
        return {
            "access_token": "pkce-token",
            "refresh_token": "pkce-refresh",
            "token_type": "Bearer",
            "scope": "reports",
            "expires_in": 3600,
        }

    def current_user(self) -> dict[str, object]:
        return {
            "id": 55,
            "name": "Auro",
            "avatar": "https://assets.example/avatar.png",
        }

    def probe_live_user_api(self) -> dict[str, object]:
        return self.current_user()

    def rate_limit(self) -> dict[str, object]:
        return {
            "limitPerHour": 3600,
            "pointsSpentThisHour": 42,
            "pointsResetIn": 1800,
        }

    def probe_live_public_api(self) -> dict[str, object]:
        return self.rate_limit()

    def regions(self) -> list[dict[str, object]]:
        return [
            {"id": 1, "compactName": "US", "name": "North America", "slug": "us"},
            {"id": 2, "compactName": "EU", "name": "Europe", "slug": "eu"},
        ]

    def expansions(self) -> list[dict[str, object]]:
        return [
            {
                "id": 12,
                "name": "Midnight",
                "zones": [
                    {"id": 38, "name": "Manaforge Omega", "frozen": False},
                ],
            }
        ]

    def server(self, *, region: str, slug: str) -> dict[str, object]:
        assert region == "us"
        assert slug == "illidan"
        return {
            "id": 10,
            "name": "Illidan",
            "normalizedName": "Illidan",
            "slug": "illidan",
            "region": {"id": 1, "compactName": "US", "name": "North America", "slug": "us"},
            "subregion": {"id": 100, "name": "Chicago"},
            "connectedRealmID": 57,
            "seasonID": 3,
        }

    def zones(self, *, expansion_id: int | None = None) -> list[dict[str, object]]:
        assert expansion_id == 12
        return [
            {
                "id": 38,
                "name": "Manaforge Omega",
                "frozen": False,
                "expansion": {"id": 12, "name": "Midnight"},
                "difficulties": [{"id": 5, "name": "Mythic", "sizes": []}],
                "encounters": [{"id": 3012, "name": "Dimensius", "journalID": 9001}],
            }
        ]

    def encounter(self, *, encounter_id: int) -> dict[str, object]:
        assert encounter_id == 3012
        return {
            "id": 3012,
            "name": "Dimensius, the All-Devouring",
            "journalID": 9001,
            "zone": {"id": 38, "name": "Manaforge Omega", "expansion": {"id": 12, "name": "Midnight"}},
        }

    def encounter_rankings(self, *, encounter_id: int, options: object) -> dict[str, object]:
        assert encounter_id == 3012
        assert options.bracket == 1
        assert options.difficulty == 5
        assert options.metric == "dps"
        assert options.page in {1, 2}
        assert options.partition == 2
        assert options.size == 20
        assert options.server_region == "us"
        assert options.server_slug == "illidan"
        assert options.leaderboard == "LogsOnly"
        assert options.hard_mode_level == "NormalMode"
        assert options.filter == "duration>120"
        assert options.include_combatant_info is True
        assert options.include_other_players is True
        assert options.class_name == "Druid"
        assert options.spec_name == "Balance"
        rankings = [
            {
                "name": "Moonkinone",
                "server": {"id": 10, "name": "Illidan", "region": "US"},
                "guild": {"id": 77, "name": "Liquid", "faction": 0},
                "class": "Druid",
                "spec": "Balance",
                "amount": 3456789.1,
                "duration": 222000,
                "report": {"code": "abcd1234", "fightID": 1, "startTime": 111},
                "talents": [],
                "gear": [],
                "externalBuffs": [],
                "allCharacters": [{"name": "Moonkinone"}, {"name": "Sherway"}, {"name": "Max"}],
            },
            {
                "name": "Moonkintwo",
                "serverName": "Tichondrius",
                "serverRegion": "US",
                "guildName": "Pieces",
                "className": "Druid",
                "spec": "Balance",
                "amount": 3344556.7,
                "duration": 224000,
                "reportCode": "wxyz9876",
                "fightID": 7,
            },
        ]
        if options.page == 2:
            rankings = [
                {
                    "name": "Moonkinthree",
                    "serverName": "Area 52",
                    "serverRegion": "US",
                    "guildName": "Big Pull",
                    "className": "Druid",
                    "spec": "Balance",
                    "amount": 3234567.8,
                    "duration": 225000,
                    "reportCode": "lmno2468",
                    "fightID": 9,
                }
            ]
        return {
            "id": 3012,
            "name": "Dimensius, the All-Devouring",
            "journalID": 9001,
            "zone": {"id": 38, "name": "Manaforge Omega", "expansion": {"id": 12, "name": "Midnight"}},
            "characterRankings": {
                "page": options.page,
                "count": len(rankings),
                "hasMorePages": options.page == 1,
                "rankings": rankings,
            },
        }

    def zone(self, *, zone_id: int) -> dict[str, object]:
        assert zone_id == 38
        return {
            "id": 38,
            "name": "Manaforge Omega",
            "frozen": False,
            "expansion": {"id": 12, "name": "Midnight"},
            "difficulties": [{"id": 5, "name": "Mythic", "sizes": []}],
            "encounters": [{"id": 3012, "name": "Dimensius", "journalID": 9001}],
            "partitions": [{"id": 1, "name": "Default", "compactName": "Default", "default": True}],
        }

    def guild(self, *, region: str, realm: str, name: str, zone_id: int | None = None) -> dict[str, object]:
        assert region == "us"
        assert realm == "illidan"
        assert name == "Liquid"
        assert zone_id == 38
        return {
            "id": 5,
            "name": "Liquid",
            "description": "Top raiding guild",
            "competitionMode": False,
            "stealthMode": False,
            "tags": ["hall-of-fame"],
            "faction": {"id": 1, "name": "Horde"},
            "server": {
                "id": 10,
                "name": "Illidan",
                "normalizedName": "Illidan",
                "slug": "illidan",
                "region": {"id": 1, "compactName": "US", "name": "North America", "slug": "us"},
                "subregion": {"id": 100, "name": "Chicago"},
                "connectedRealmID": 57,
                "seasonID": 3,
            },
            "zoneRanking": {
                "progress": {
                    "worldRank": {"number": 2, "color": "legendary", "percentile": None},
                    "regionRank": {"number": 1, "color": "legendary", "percentile": None},
                    "serverRank": {"number": 1, "color": "legendary", "percentile": None},
                }
            },
        }

    def guild_rankings(
        self,
        *,
        region: str,
        realm: str,
        name: str,
        zone_id: int | None = None,
        size: int | None = None,
        difficulty: int | None = None,
    ) -> dict[str, object]:
        assert region == "us"
        assert realm == "illidan"
        assert name == "Liquid"
        assert zone_id == 38
        assert size == 20
        assert difficulty == 5
        return {
            "id": 5,
            "name": "Liquid",
            "server": {
                "id": 10,
                "name": "Illidan",
                "normalizedName": "Illidan",
                "slug": "illidan",
                "region": {"id": 1, "compactName": "US", "name": "North America", "slug": "us"},
                "subregion": {"id": 100, "name": "Chicago"},
            },
            "zoneRanking": {
                "progress": {
                    "worldRank": {"number": 2, "color": "legendary", "percentile": None},
                    "regionRank": {"number": 1, "color": "legendary", "percentile": None},
                    "serverRank": {"number": 1, "color": "legendary", "percentile": None},
                },
                "speed": {
                    "worldRank": {"number": 4, "color": "epic", "percentile": None},
                    "regionRank": {"number": 2, "color": "epic", "percentile": None},
                    "serverRank": {"number": 1, "color": "epic", "percentile": None},
                },
                "completeRaidSpeed": None,
            },
        }

    def guild_members(
        self,
        *,
        region: str,
        realm: str,
        name: str,
        limit: int = 100,
        page: int = 1,
    ) -> dict[str, object]:
        assert region == "us"
        assert realm == "illidan"
        assert name == "Liquid"
        assert limit == 2
        assert page == 1
        return {
            "id": 5,
            "name": "Liquid",
            "server": {
                "id": 10,
                "name": "Illidan",
                "normalizedName": "Illidan",
                "slug": "illidan",
                "region": {"id": 1, "compactName": "US", "name": "North America", "slug": "us"},
                "subregion": {"id": 100, "name": "Chicago"},
            },
            "members": {
                "data": [
                    {
                        "id": 77,
                        "canonicalID": 88,
                        "name": "Roguecane",
                        "level": 80,
                        "classID": 4,
                        "hidden": False,
                        "guildRank": 3,
                        "faction": {"id": 1, "name": "Horde"},
                        "server": {
                            "id": 10,
                            "name": "Illidan",
                            "normalizedName": "Illidan",
                            "slug": "illidan",
                            "region": {"id": 1, "compactName": "US", "name": "North America", "slug": "us"},
                            "subregion": {"id": 100, "name": "Chicago"},
                            "connectedRealmID": 57,
                            "seasonID": 3,
                        },
                    }
                ],
                "total": 1,
                "per_page": 2,
                "current_page": 1,
                "from": 1,
                "to": 1,
                "last_page": 1,
                "has_more_pages": False,
            },
        }

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
    ) -> dict[str, object]:
        assert region == "us"
        assert realm == "illidan"
        assert name == "Liquid"
        assert guild_tag_id == 5
        assert limit == 2
        assert page == 1
        assert zone_id == 38
        return {
            "id": 5,
            "name": "Liquid",
            "server": {
                "id": 10,
                "name": "Illidan",
                "normalizedName": "Illidan",
                "slug": "illidan",
                "region": {"id": 1, "compactName": "US", "name": "North America", "slug": "us"},
                "subregion": {"id": 100, "name": "Chicago"},
            },
            "attendance": {
                "data": [
                    {
                        "code": "ABCD1234",
                        "startTime": 1234567890,
                        "zone": {"id": 38, "name": "Manaforge Omega", "frozen": False},
                        "players": [
                            {"name": "Roguecane", "type": "Rogue", "presence": 1},
                            {"name": "Benchlock", "type": "Warlock", "presence": 2},
                        ],
                    }
                ],
                "total": 1,
                "per_page": 2,
                "current_page": 1,
                "from": 1,
                "to": 1,
                "last_page": 1,
                "has_more_pages": False,
            },
        }

    def character(self, *, region: str, realm: str, name: str) -> dict[str, object]:
        assert region == "us"
        assert realm == "illidan"
        assert name == "Roguecane"
        return {
            "id": 77,
            "canonicalID": 88,
            "name": "Roguecane",
            "level": 80,
            "classID": 4,
            "hidden": False,
            "faction": {"id": 1, "name": "Horde"},
            "guildRank": 3,
            "server": {
                "id": 10,
                "name": "Illidan",
                "normalizedName": "Illidan",
                "slug": "illidan",
                "region": {"id": 1, "compactName": "US", "name": "North America", "slug": "us"},
                "subregion": {"id": 100, "name": "Chicago"},
                "connectedRealmID": 57,
                "seasonID": 3,
            },
            "guilds": [
                {
                    "id": 5,
                    "name": "Liquid",
                    "server": {
                        "id": 10,
                        "name": "Illidan",
                        "normalizedName": "Illidan",
                        "slug": "illidan",
                        "region": {"id": 1, "compactName": "US", "name": "North America", "slug": "us"},
                        "subregion": {"id": 100, "name": "Chicago"},
                        "connectedRealmID": 57,
                        "seasonID": 3,
                    },
                }
            ],
        }

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
    ) -> dict[str, object]:
        assert region == "us"
        assert realm == "illidan"
        assert name == "Roguecane"
        assert zone_id == 38
        assert difficulty == 5
        assert metric == "dps"
        assert size == 20
        assert spec_name == "assassination"
        return {
            "id": 77,
            "canonicalID": 88,
            "name": "Roguecane",
            "level": 80,
            "classID": 4,
            "faction": {"id": 1, "name": "Horde"},
            "server": {
                "id": 10,
                "name": "Illidan",
                "normalizedName": "Illidan",
                "slug": "illidan",
                "region": {"id": 1, "compactName": "US", "name": "North America", "slug": "us"},
                "subregion": {"id": 100, "name": "Chicago"},
            },
            "zoneRankings": {
                "zone": 38,
                "difficulty": 5,
                "metric": "dps",
                "partition": 1,
                "size": 20,
                "bestPerformanceAverage": 85.4,
                "medianPerformanceAverage": 77.2,
                "allStars": [
                    {
                        "spec": "Assassination",
                        "points": 263.29,
                        "possiblePoints": 960,
                        "rank": 21679,
                        "rankPercent": 37.7,
                        "regionRank": 7393,
                        "serverRank": 660,
                        "total": 34818,
                    }
                ],
                "rankings": [
                    {
                        "encounter": {"id": 3012, "name": "Dimensius"},
                        "spec": "Assassination",
                        "bestSpec": "Assassination",
                        "rankPercent": 65.7,
                        "medianPercent": 65.7,
                        "totalKills": 1,
                        "allStars": {"points": 66.06},
                        "bestRank": {"rank_id": 123},
                        "bestAmount": 2999570.9,
                        "fastestKill": 223706,
                    }
                ],
            },
        }

    def report(self, *, code: str, allow_unlisted: bool = False) -> dict[str, object]:
        assert code == "abcd1234"
        assert allow_unlisted in {True, False}
        return {
            "code": "abcd1234",
            "title": "Manaforge Omega - Liquid",
            "startTime": 123,
            "endTime": 456,
            "visibility": "public",
            "archiveStatus": {
                "isArchived": True,
                "isAccessible": True,
                "archiveDate": 789,
            },
            "segments": 1,
            "exportedSegments": 0,
            "zone": {"id": 38, "name": "Manaforge Omega"},
            "guild": {
                "id": 5,
                "name": "Liquid",
                "server": {
                    "id": 10,
                    "name": "Illidan",
                    "normalizedName": "Illidan",
                    "slug": "illidan",
                    "region": {"id": 1, "compactName": "US", "name": "North America", "slug": "us"},
                    "subregion": {"id": 100, "name": "Chicago"},
                    "connectedRealmID": 57,
                    "seasonID": 3,
                },
            },
        }

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
    ) -> dict[str, object]:
        if guild_region == "us" and guild_realm == "illidan" and guild_name == "Liquid":
            assert limit == 10
            assert page == 2
            assert zone_id == 38
            assert game_zone_id == 12961
            assert start_time == 1000.0
            assert end_time == 2000.0
            return {
                "data": [
                    {
                        "code": "abcd1234",
                        "title": "Manaforge Omega - Liquid",
                        "startTime": 123,
                        "endTime": 456,
                        "visibility": "public",
                        "archiveStatus": {
                            "isArchived": True,
                            "isAccessible": True,
                            "archiveDate": 789,
                        },
                        "segments": 1,
                        "exportedSegments": 0,
                        "zone": {"id": 38, "name": "Manaforge Omega"},
                        "guild": {
                            "id": 5,
                            "name": "Liquid",
                            "server": {
                                "id": 10,
                                "name": "Illidan",
                                "normalizedName": "Illidan",
                                "slug": "illidan",
                                "region": {"id": 1, "compactName": "US", "name": "North America", "slug": "us"},
                                "subregion": {"id": 100, "name": "Chicago"},
                            },
                        },
                    }
                ],
                "total": 25,
                "per_page": 10,
                "current_page": 2,
                "from": 11,
                "to": 20,
                "last_page": 3,
                "has_more_pages": True,
            }

        assert zone_id == 38
        assert game_zone_id is None
        return {
            "data": [
                {
                    "code": "abcd1234",
                    "title": "Manaforge Omega - Liquid",
                    "startTime": 123,
                    "endTime": 456,
                    "visibility": "public",
                    "archiveStatus": {
                        "isArchived": True,
                        "isAccessible": True,
                        "archiveDate": 789,
                    },
                    "segments": 1,
                    "exportedSegments": 0,
                    "zone": {"id": 38, "name": "Manaforge Omega"},
                    "guild": {
                        "id": 5,
                        "name": "Liquid",
                        "server": {
                            "id": 10,
                            "name": "Illidan",
                            "normalizedName": "Illidan",
                            "slug": "illidan",
                            "region": {"id": 1, "compactName": "US", "name": "North America", "slug": "us"},
                            "subregion": {"id": 100, "name": "Chicago"},
                        },
                    },
                },
                {
                    "code": "live9999",
                    "title": "Manaforge Omega - Live Pull",
                    "startTime": 789,
                    "endTime": None,
                    "visibility": "public",
                    "archiveStatus": {
                        "isArchived": False,
                        "isAccessible": True,
                        "archiveDate": None,
                    },
                    "segments": 1,
                    "exportedSegments": 0,
                    "zone": {"id": 38, "name": "Manaforge Omega"},
                    "guild": {
                        "id": 6,
                        "name": "Echo",
                        "server": {
                            "id": 11,
                            "name": "Tarren Mill",
                            "normalizedName": "Tarren Mill",
                            "slug": "tarren-mill",
                            "region": {"id": 2, "compactName": "EU", "name": "Europe", "slug": "eu"},
                            "subregion": {"id": 101, "name": "Paris"},
                        },
                    },
                },
            ],
            "total": 2,
            "per_page": limit,
            "current_page": page,
            "from": 1,
            "to": 2,
            "last_page": 1,
            "has_more_pages": False,
        }

    def report_fights(self, *, code: str, difficulty: int | None = None, allow_unlisted: bool = False, ttl_override: int | None = None) -> dict[str, object]:
        assert code == "abcd1234"
        assert difficulty in {None, 5}
        assert allow_unlisted is False
        return {
            "code": "abcd1234",
            "title": "Manaforge Omega - Liquid",
            "zone": {"id": 38, "name": "Manaforge Omega"},
            "fights": [
                {
                    "id": 1,
                    "name": "Dimensius, the All-Devouring",
                    "encounterID": 3012,
                    "difficulty": 5,
                    "kill": True,
                    "completeRaid": False,
                    "startTime": 100000,
                    "endTime": 200000,
                    "fightPercentage": 100,
                    "bossPercentage": 0,
                    "averageItemLevel": 685.2,
                    "size": 20,
                },
                {
                    "id": 2,
                    "name": "Dimensius, the All-Devouring",
                    "encounterID": 3012,
                    "difficulty": 5,
                    "kill": False,
                    "completeRaid": False,
                    "startTime": 300000,
                    "endTime": 700000,
                    "fightPercentage": 12.4,
                    "bossPercentage": 12.4,
                    "averageItemLevel": 685.2,
                    "size": 20,
                },
                {
                    "id": 3,
                    "name": "Forgeweaver Araz",
                    "encounterID": 3002,
                    "difficulty": 5,
                    "kill": True,
                    "completeRaid": False,
                    "startTime": 800000,
                    "endTime": 1250000,
                    "fightPercentage": 100,
                    "bossPercentage": 0,
                    "averageItemLevel": 685.2,
                    "size": 20,
                }
            ],
        }

    def report_events(self, *, code: str, allow_unlisted: bool = False, options) -> dict[str, object]:  # noqa: ANN001
        assert code == "abcd1234"
        assert allow_unlisted is False
        assert options.data_type == "Casts"
        if options.ability_id is not None:
            assert options.ability_id == 20473.0
            assert options.encounter_id == 3012
            assert options.fight_ids == [1]
            assert options.kill_type == "Kills"
            assert options.limit == 200
            rows = [
                {"type": "cast", "timestamp": 120000, "sourceID": 9, "targetID": 501, "abilityGameID": 20473},
                {"type": "cast", "timestamp": 145000, "sourceID": 9, "targetID": 501, "abilityGameID": 20473},
            ]
            return {
                "code": "abcd1234",
                "title": "Manaforge Omega - Liquid",
                "zone": {"id": 38, "name": "Manaforge Omega"},
                "events": {
                    "data": rows,
                    "nextPageTimestamp": 999.0,
                },
            }
        assert options.encounter_id == 3012
        if options.fight_ids == [1, 2]:
            assert options.difficulty == 5
            assert options.limit == 50
            assert options.source_id == 9
            rows = [{"type": "cast", "abilityGameID": 12345}]
        else:
            assert options.fight_ids == [1]
            assert options.limit == 200
            assert options.hostility_type in {None, "Friendlies"}
            if options.start_time is not None or options.end_time is not None:
                assert options.start_time == 105000.0
                assert options.end_time == 130000.0
            rows = [
                {"type": "cast", "timestamp": 120000, "sourceID": 9, "targetID": 501, "abilityGameID": 20473},
                {"type": "cast", "timestamp": 145000, "sourceID": 9, "targetID": 501, "abilityGameID": 20473},
                {"type": "cast", "timestamp": 160000, "sourceID": 1, "targetID": 777, "abilityGameID": 57795},
            ]
        return {
            "code": "abcd1234",
            "title": "Manaforge Omega - Liquid",
            "zone": {"id": 38, "name": "Manaforge Omega"},
            "events": {
                "data": rows,
                "nextPageTimestamp": 999.0,
            },
        }

    def report_table(self, *, code: str, allow_unlisted: bool = False, options) -> dict[str, object]:  # noqa: ANN001
        assert code == "abcd1234"
        assert options.data_type in {"DamageDone", "Buffs"}
        if options.encounter_id == 3012:
            assert allow_unlisted is False
            assert options.fight_ids == [1]
            assert options.kill_type == "Kills"
            if options.data_type == "Buffs" and options.ability_id is not None:
                assert options.view_by == "Source"
                assert options.ability_id == 20473.0
                if options.start_time == 150000.0 and options.end_time == 190000.0:
                    entries = [
                        {
                            "id": 9,
                            "name": "Auropower",
                            "total": 65.0,
                            "activeTime": 26000,
                            "totalTime": 40000,
                            "bands": [{"startTime": 152000, "endTime": 188000}],
                        },
                        {
                            "id": 1,
                            "name": "Sherway",
                            "total": 80.0,
                            "activeTime": 32000,
                            "totalTime": 40000,
                            "bands": [{"startTime": 151000, "endTime": 189000}],
                        },
                    ]
                    return {
                        "code": "abcd1234",
                        "title": "Manaforge Omega - Liquid",
                        "zone": {"id": 38, "name": "Manaforge Omega"},
                        "table": {"entries": entries},
                    }
                entries = [
                    {
                        "id": 9,
                        "name": "Auropower",
                        "total": 98.7,
                        "activeTime": 74000,
                        "totalTime": 75000,
                        "bands": [{"startTime": 110000, "endTime": 150000}],
                    },
                    {
                        "id": 1,
                        "name": "Sherway",
                        "total": 45.2,
                        "activeTime": 33900,
                        "totalTime": 75000,
                        "bands": [{"startTime": 118000, "endTime": 140000}],
                    },
                ]
                return {
                    "code": "abcd1234",
                    "title": "Manaforge Omega - Liquid",
                    "zone": {"id": 38, "name": "Manaforge Omega"},
                    "table": {"entries": entries},
                }
            if options.data_type == "DamageDone":
                assert options.view_by in {"Source", "Target"}
                if options.view_by == "Target":
                    entries = [
                        {"id": 501, "name": "Dimensius, the All-Devouring", "total": 210000},
                        {"id": 777, "name": "Unstable Voidling", "total": 13456},
                    ]
                    return {
                        "code": "abcd1234",
                        "title": "Manaforge Omega - Liquid",
                        "zone": {"id": 38, "name": "Manaforge Omega"},
                        "table": {"entries": entries},
                    }
                entries = [
                    {"id": 9, "name": "Auropower", "total": 123456},
                    {"id": 1, "name": "Sherway", "total": 100000},
                ]
                return {
                    "code": "abcd1234",
                    "title": "Manaforge Omega - Liquid",
                    "zone": {"id": 38, "name": "Manaforge Omega"},
                    "table": {"entries": entries},
                }
            if options.start_time is not None or options.end_time is not None:
                assert options.start_time == 110000.0
                assert options.end_time == 150000.0
            entries = [{"name": "Auropower", "total": 123456 if options.data_type == "DamageDone" else 98.7}]
        else:
            assert allow_unlisted is True
            assert options.data_type == "DamageDone"
            assert options.view_by == "Source"
            entries = [{"name": "Auropower", "total": 123456}]
        return {
            "code": "abcd1234",
            "title": "Manaforge Omega - Liquid",
            "zone": {"id": 38, "name": "Manaforge Omega"},
            "table": {"entries": entries},
        }

    def report_graph(self, *, code: str, allow_unlisted: bool = False, options) -> dict[str, object]:  # noqa: ANN001
        assert code == "abcd1234"
        assert allow_unlisted is False
        assert options.data_type == "DamageDone"
        assert options.view_by == "Target"
        return {
            "code": "abcd1234",
            "title": "Manaforge Omega - Liquid",
            "zone": {"id": 38, "name": "Manaforge Omega"},
            "graph": {"series": [{"name": "Damage", "data": [1, 2, 3]}]},
        }

    def report_master_data(
        self,
        *,
        code: str,
        allow_unlisted: bool = False,
        translate: bool | None = None,
        actor_type: str | None = None,
        actor_sub_type: str | None = None,
    ) -> dict[str, object]:
        assert code == "abcd1234"
        assert allow_unlisted is False
        assert actor_type in {None, "Player"}
        if actor_sub_type is not None:
            assert translate is False
            assert actor_sub_type == "Paladin"
        return {
            "code": "abcd1234",
            "title": "Manaforge Omega - Liquid",
            "zone": {"id": 38, "name": "Manaforge Omega"},
            "masterData": {
                "logVersion": 47,
                "gameVersion": 120001,
                "lang": "en",
                "abilities": [
                    {"gameID": 20473, "icon": "spell_holy_holybolt", "name": "Holy Shock", "type": "Holy"},
                    {"gameID": 57795, "icon": "ability_paladin_judgementblue", "name": "Judgment", "type": "Holy"},
                ],
                "actors": [
                    {
                        "gameID": 0,
                        "icon": "classicon_paladin",
                        "id": 9,
                        "name": "Auropower",
                        "petOwner": None,
                        "server": "Mal'Ganis",
                        "subType": "Paladin",
                        "type": "Player",
                    },
                    {
                        "gameID": 0,
                        "icon": "classicon_warrior",
                        "id": 1,
                        "name": "Sherway",
                        "petOwner": None,
                        "server": "Illidan",
                        "subType": "Warrior",
                        "type": "Player",
                    },
                    {
                        "gameID": 0,
                        "icon": "inv_misc_questionmark",
                        "id": 501,
                        "name": "Dimensius, the All-Devouring",
                        "petOwner": None,
                        "server": None,
                        "subType": "Boss",
                        "type": "NPC",
                    },
                    {
                        "gameID": 0,
                        "icon": "inv_misc_questionmark",
                        "id": 777,
                        "name": "Unstable Voidling",
                        "petOwner": None,
                        "server": None,
                        "subType": "Add",
                        "type": "NPC",
                    },
                ],
            },
        }

    def report_player_details(self, *, code: str, allow_unlisted: bool = False, options, ttl_override: int | None = None) -> dict[str, object]:  # noqa: ANN001
        assert code == "abcd1234"
        assert allow_unlisted is False
        assert options.encounter_id == 3012
        assert options.fight_ids in ([1, 2], [1])
        assert options.kill_type == "Kills"
        if options.fight_ids == [1, 2]:
            assert options.difficulty == 5
            assert options.include_combatant_info is True
        else:
            assert options.difficulty in {None, 5}
        return {
            "code": "abcd1234",
            "title": "Manaforge Omega - Liquid",
            "zone": {"id": 38, "name": "Manaforge Omega"},
            "playerDetails": {
                "data": {
                    "tanks": [{"name": "Sherway", "id": 1, "type": "Warrior", "specs": [{"spec": "Protection", "count": 1}]}],
                    "healers": [],
                    "dps": [
                        {
                            "name": "Auropower",
                            "id": 9,
                            "type": "Paladin",
                            "specs": [{"spec": "Retribution", "count": 1}],
                            "combatantInfo": {
                                "talentTree": [
                                    {"id": 103324, "nodeID": 82244, "rank": 1},
                                    {"id": 109839, "nodeID": 88206, "rank": 1},
                                    {"id": 117176, "nodeID": 94585, "rank": 1},
                                ]
                            },
                        }
                    ],
                }
            },
        }

    def report_rankings(self, *, code: str, allow_unlisted: bool = False, options) -> dict[str, object]:  # noqa: ANN001
        assert code == "abcd1234"
        assert allow_unlisted is True
        assert options.compare == "Rankings"
        assert options.difficulty == 5
        assert options.encounter_id == 3012
        assert options.fight_ids == [1, 2]
        assert options.player_metric == "dps"
        assert options.timeframe == "Historical"
        return {
            "code": "abcd1234",
            "title": "Manaforge Omega - Liquid",
            "zone": {"id": 38, "name": "Manaforge Omega"},
            "rankings": {
                "data": [
                    {"name": "Auropower", "amount": 123456, "rankPercent": 95.2, "bracketData": {"size": 20}},
                ]
            },
        }


def test_encounter_rankings_request_inlines_leaderboard_enum_and_omits_null_filters() -> None:
    query, variables = _encounter_rankings_request(
        encounter_id=3012,
        options=EncounterRankingsOptions(difficulty=5, leaderboard="LogsOnly"),
    )
    assert "$leaderboard" not in query
    assert "leaderboard: LogsOnly" in query
    assert "$difficulty: Int" in query
    assert "$bracket" not in query
    assert variables == {"id": 3012, "difficulty": 5}


def test_warcraftlogs_client_cache_key_includes_dynamic_query_text() -> None:
    client = WarcraftLogsClient.__new__(WarcraftLogsClient)
    client._site = RETAIL_PROFILE

    logs_query, logs_variables = _encounter_rankings_request(
        encounter_id=3012,
        options=EncounterRankingsOptions(difficulty=5, leaderboard="LogsOnly"),
    )
    speed_query, speed_variables = _encounter_rankings_request(
        encounter_id=3012,
        options=EncounterRankingsOptions(difficulty=5, leaderboard="Speed"),
    )

    logs_key = client._cache_key(
        "encounter_rankings",
        {"operation_name": "EncounterRankings", "query": logs_query, "variables": logs_variables},
    )
    speed_key = client._cache_key(
        "encounter_rankings",
        {"operation_name": "EncounterRankings", "query": speed_query, "variables": speed_variables},
    )

    assert logs_variables == {"id": 3012, "difficulty": 5}
    assert speed_variables == {"id": 3012, "difficulty": 5}
    assert logs_query != speed_query
    assert logs_key != speed_key


def test_warcraftlogs_client_encounter_rankings_preserves_display_name_filters() -> None:
    captured: dict[str, object] = {}
    client = WarcraftLogsClient.__new__(WarcraftLogsClient)
    client._guild_ttl = 300

    def _fake_graphql(
        *,
        operation_name: str,
        query: str,
        variables: dict[str, object],
        namespace: str,
        ttl_seconds: int,
    ) -> dict[str, object]:
        captured["operation_name"] = operation_name
        captured["query"] = query
        captured["variables"] = variables
        captured["namespace"] = namespace
        captured["ttl_seconds"] = ttl_seconds
        return {
            "worldData": {
                "encounter": {
                    "id": 3012,
                    "name": "Dimensius, the All-Devouring",
                    "journalID": 9001,
                    "zone": {"id": 38, "name": "Manaforge Omega", "expansion": {"id": 12, "name": "Midnight"}},
                    "characterRankings": {"rankings": []},
                }
            }
        }

    client._graphql = _fake_graphql

    payload = client.encounter_rankings(
        encounter_id=3012,
        options=EncounterRankingsOptions(class_name="Hunter", spec_name="Marksmanship"),
    )

    assert payload["id"] == 3012
    assert captured["operation_name"] == "EncounterRankings"
    assert captured["namespace"] == "encounter_rankings"
    assert captured["ttl_seconds"] == 300
    assert captured["variables"] == {"id": 3012, "className": "Hunter", "specName": "Marksmanship"}


def test_warcraftlogs_doctor_reports_phase_one_capabilities(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _FakeWarcraftLogsClient)
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": True, "env_file": "/tmp/.env.local"})(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": False,
            "readable": False,
            "valid_json": False,
            "auth_mode": None,
            "has_access_token": False,
            "has_refresh_token": False,
            "expires_at": None,
            "expired": None,
        },
    )
    result = runner.invoke(warcraftlogs_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "warcraftlogs"
    assert payload["status"] == "ready"
    assert payload["auth"]["configured"] is True
    assert payload["auth"]["client_credentials_configured"] is True
    assert payload["auth"]["credential_source"] == "/tmp/.env.local"
    assert payload["auth"]["lookup_order"][0] == ".env.local"
    assert payload["auth"]["lookup_order"][-1] == "environment"
    assert payload["auth"]["state"]["exists"] is False
    assert payload["auth"]["public_api_access"]["ready"] is True
    assert payload["auth"]["public_api_access"]["mode"] == "client_credentials"
    assert payload["auth"]["public_api_access"]["validation"] == "live"
    assert payload["auth"]["public_api_access"]["probe"] == "rate_limit"
    assert payload["auth"]["user_api_access"]["ready"] is False
    assert payload["capabilities"]["guild"] == "ready"
    assert payload["capabilities"]["search"] == "ready_explicit_report_only"
    assert payload["capabilities"]["resolve"] == "ready_explicit_report_only"
    assert payload["capabilities"]["report_fights"] == "ready"
    assert payload["capabilities"]["boss_spec_usage"] == "ready"
    assert payload["capabilities"]["comp_samples"] == "ready"
    assert payload["capabilities"]["ability_usage_summary"] == "ready"
    assert payload["capabilities"]["report_encounter_buffs"] == "ready"
    assert payload["capabilities"]["report_encounter_aura_summary"] == "ready"
    assert payload["capabilities"]["report_encounter_aura_compare"] == "ready"
    assert payload["capabilities"]["report_encounter_damage_source_summary"] == "ready"
    assert payload["capabilities"]["report_encounter_damage_target_summary"] == "ready"
    assert payload["capabilities"]["report_encounter_damage_breakdown"] == "ready"
    assert payload["capabilities"]["user_auth"] == "ready_manual_exchange"


def test_warcraftlogs_doctor_reports_saved_user_token_runtime_access(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _FakeWarcraftLogsClient)
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": False, "env_file": None})(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": True,
            "readable": True,
            "valid_json": True,
            "auth_mode": "pkce",
            "pending_auth_mode": None,
            "has_pending_state": False,
            "has_access_token": True,
            "has_refresh_token": True,
            "expires_at": 1500.0,
            "expired": False,
        },
    )

    result = runner.invoke(warcraftlogs_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["auth"]["configured"] is False
    assert payload["auth"]["public_api_access"]["ready"] is False
    assert payload["auth"]["public_api_access"]["mode"] is None
    assert payload["auth"]["public_api_access"]["reason"] == "requires_client_credentials"
    assert payload["auth"]["public_api_access"]["validation"] == "local"
    assert payload["auth"]["user_api_access"]["ready"] is True
    assert payload["auth"]["user_api_access"]["validation"] == "live"
    assert payload["auth"]["user_api_access"]["probe"] == "current_user"
    assert payload["capabilities"]["report_fights"] == "requires_client_credentials"
    assert payload["capabilities"]["user_auth"] == "ready"


def test_warcraftlogs_doctor_requires_client_credentials_for_user_auth_bootstrap(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _FakeWarcraftLogsClient)
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": False, "env_file": None})(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": False,
            "readable": False,
            "valid_json": False,
            "auth_mode": None,
            "pending_auth_mode": None,
            "has_pending_state": False,
            "has_access_token": False,
            "has_refresh_token": False,
            "expires_at": None,
            "expired": None,
        },
    )

    result = runner.invoke(warcraftlogs_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["auth"]["user_api_access"]["ready"] is False
    assert payload["capabilities"]["user_auth"] == "requires_client_credentials"


def test_warcraftlogs_doctor_can_skip_live_probes(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _FakeWarcraftLogsClient)
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": True, "env_file": "/tmp/.env.local"})(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": False,
            "readable": False,
            "valid_json": False,
            "auth_mode": None,
            "has_access_token": False,
            "has_refresh_token": False,
            "expires_at": None,
            "expired": None,
        },
    )

    result = runner.invoke(warcraftlogs_app, ["doctor", "--no-live"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["auth"]["public_api_access"]["ready"] is True
    assert payload["auth"]["public_api_access"]["validation"] == "skipped"
    assert payload["auth"]["public_api_access"]["live_validated"] is False
    assert payload["capabilities"]["report_fights"] == "ready"
    assert payload["capabilities"]["user_auth"] == "ready_manual_exchange"


def test_warcraftlogs_doctor_live_probe_uses_uncached_public_helper(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": True, "env_file": "/tmp/.env.local"})(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": False,
            "readable": False,
            "valid_json": False,
            "auth_mode": None,
            "has_access_token": False,
            "has_refresh_token": False,
            "expires_at": None,
            "expired": None,
        },
    )

    class _ProbeAwareClient(_FakeWarcraftLogsClient):
        def rate_limit(self) -> dict[str, object]:
            raise AssertionError("doctor should use the uncached public probe helper")

        def probe_live_public_api(self) -> dict[str, object]:
            return {
                "limitPerHour": 3600,
                "pointsSpentThisHour": 42,
                "pointsResetIn": 1800,
            }

    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _ProbeAwareClient)

    result = runner.invoke(warcraftlogs_app, ["doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["auth"]["public_api_access"]["ready"] is True
    assert payload["auth"]["public_api_access"]["validation"] == "live"
    assert payload["auth"]["public_api_access"]["live_validated"] is True


def test_warcraftlogs_doctor_reports_live_public_auth_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": True, "env_file": "/tmp/.env.local"})(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": False,
            "readable": False,
            "valid_json": False,
            "auth_mode": None,
            "has_access_token": False,
            "has_refresh_token": False,
            "expires_at": None,
            "expired": None,
        },
    )

    class _AuthFailingPublicClient(_FakeWarcraftLogsClient):
        def rate_limit(self) -> dict[str, object]:
            raise WarcraftLogsClientError("auth_failed", "Warcraft Logs rejected the client credentials.")

    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _AuthFailingPublicClient)

    result = runner.invoke(warcraftlogs_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["auth"]["public_api_access"]["ready"] is False
    assert payload["auth"]["public_api_access"]["reason"] == "auth_failed"
    assert payload["auth"]["public_api_access"]["validation"] == "live"
    assert payload["capabilities"]["report_fights"] == "auth_failed"


def test_warcraftlogs_doctor_reports_invalid_runtime_config(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": True, "env_file": "/tmp/.env.local"})(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": False,
            "readable": False,
            "valid_json": False,
            "auth_mode": None,
            "pending_auth_mode": None,
            "has_pending_state": False,
            "has_access_token": False,
            "has_refresh_token": False,
            "expires_at": None,
            "expired": None,
        },
    )

    class _BrokenClient:
        def __init__(self) -> None:
            raise ValueError("WOWHEAD_REDIS_URL is required when WOWHEAD_CACHE_BACKEND=redis.")

    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _BrokenClient)

    result = runner.invoke(warcraftlogs_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["auth"]["runtime_access"]["ready"] is False
    assert payload["auth"]["runtime_access"]["reason"] == "invalid_runtime_config"
    assert "WARCRAFTLOGS_REDIS_URL" in payload["auth"]["runtime_access"]["message"]
    assert "WOWHEAD_REDIS_URL" not in payload["auth"]["runtime_access"]["message"]
    assert payload["auth"]["public_api_access"]["ready"] is False
    assert payload["auth"]["public_api_access"]["reason"] == "invalid_runtime_config"
    assert payload["capabilities"]["report_fights"] == "invalid_runtime_config"
    assert payload["capabilities"]["user_auth"] == "invalid_runtime_config"


def test_warcraftlogs_doctor_reports_invalid_runtime_config_for_saved_user_token(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": False, "env_file": None})(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": True,
            "readable": True,
            "valid_json": True,
            "auth_mode": "pkce",
            "has_access_token": True,
            "has_refresh_token": True,
            "expires_at": 1500.0,
            "expired": False,
        },
    )

    class _BrokenClient:
        def __init__(self) -> None:
            raise ValueError("WOWHEAD_REDIS_URL is required when WOWHEAD_CACHE_BACKEND=redis.")

    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _BrokenClient)

    result = runner.invoke(warcraftlogs_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["auth"]["user_api_access"]["ready"] is False
    assert payload["auth"]["user_api_access"]["reason"] == "invalid_runtime_config"
    assert payload["capabilities"]["user_auth"] == "invalid_runtime_config"


def test_warcraftlogs_doctor_prioritizes_invalid_runtime_config_without_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": False, "env_file": None})(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": False,
            "readable": False,
            "valid_json": False,
            "auth_mode": None,
            "pending_auth_mode": None,
            "has_pending_state": False,
            "has_access_token": False,
            "has_refresh_token": False,
            "expires_at": None,
            "expired": None,
        },
    )

    class _BrokenClient:
        def __init__(self) -> None:
            raise ValueError("WOWHEAD_REDIS_URL is required when WOWHEAD_CACHE_BACKEND=redis.")

    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _BrokenClient)

    result = runner.invoke(warcraftlogs_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["auth"]["public_api_access"]["reason"] == "invalid_runtime_config"
    assert payload["auth"]["user_api_access"]["reason"] == "invalid_runtime_config"
    assert payload["capabilities"]["report_fights"] == "invalid_runtime_config"
    assert payload["capabilities"]["user_auth"] == "invalid_runtime_config"


def test_warcraftlogs_search_matches_explicit_report_reference() -> None:
    result = runner.invoke(warcraftlogs_app, ["search", "https://www.warcraftlogs.com/reports/abcd1234#fight=3"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "warcraftlogs"
    assert payload["count"] == 1
    assert payload["results"][0]["kind"] == "report_encounter"
    assert payload["results"][0]["report_reference"]["code"] == "abcd1234"
    assert payload["results"][0]["report_reference"]["fight_id"] == 3
    assert payload["results"][0]["follow_up"]["command"] == "warcraftlogs report-encounter abcd1234 --fight-id 3"


def test_warcraftlogs_resolve_requires_explicit_report_reference() -> None:
    result = runner.invoke(warcraftlogs_app, ["resolve", "liquid mythic report"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "warcraftlogs"
    assert payload["resolved"] is False
    assert payload["confidence"] == "none"
    assert "explicit report URL or a bare report code" in payload["message"]


def test_warcraftlogs_resolve_matches_bare_report_code() -> None:
    result = runner.invoke(warcraftlogs_app, ["resolve", "abcd1234"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "warcraftlogs"
    assert payload["resolved"] is True
    assert payload["confidence"] == "medium"
    assert payload["match"]["kind"] == "report"
    assert payload["next_command"] == "warcraftlogs report abcd1234"


def test_warcraftlogs_auth_status_reports_shared_state_summary(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _FakeWarcraftLogsClient)
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": True, "env_file": "/tmp/.env.local"})(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": True,
            "readable": True,
            "valid_json": True,
            "auth_mode": "authorization_code",
            "has_access_token": True,
            "has_refresh_token": True,
            "expires_at": 1500.0,
            "expired": False,
        },
    )

    result = runner.invoke(warcraftlogs_app, ["auth", "status"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["auth"]["configured"] is True
    assert payload["auth"]["client_credentials_configured"] is True
    assert payload["auth"]["state"]["exists"] is True
    assert payload["auth"]["state"]["auth_mode"] == "authorization_code"
    assert payload["auth"]["public_api_access"]["ready"] is True
    assert payload["auth"]["user_api_access"]["ready"] is True
    assert payload["auth"]["public_api_access"]["validation"] == "live"
    assert payload["auth"]["user_api_access"]["validation"] == "live"
    assert payload["auth"]["grants"]["client_credentials"] == "ready"
    assert payload["auth"]["grants"]["pkce"] == "ready_manual_exchange"


def test_warcraftlogs_auth_status_reports_grants_blocked_without_client_credentials(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _FakeWarcraftLogsClient)
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": False, "env_file": None})(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": True,
            "readable": True,
            "valid_json": True,
            "auth_mode": "pkce",
            "has_access_token": True,
            "has_refresh_token": True,
            "expires_at": 1500.0,
            "expired": False,
        },
    )

    result = runner.invoke(warcraftlogs_app, ["auth", "status"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["auth"]["public_api_access"]["ready"] is False
    assert payload["auth"]["user_api_access"]["ready"] is True
    assert payload["auth"]["grants"]["client_credentials"] == "requires_client_credentials"
    assert payload["auth"]["grants"]["authorization_code"] == "requires_client_credentials"
    assert payload["auth"]["grants"]["pkce"] == "requires_client_credentials"


def test_warcraftlogs_auth_status_can_skip_live_probes(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _FakeWarcraftLogsClient)
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": True, "env_file": "/tmp/.env.local"})(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": True,
            "readable": True,
            "valid_json": True,
            "auth_mode": "pkce",
            "has_access_token": True,
            "has_refresh_token": True,
            "expires_at": 1500.0,
            "expired": False,
        },
    )

    result = runner.invoke(warcraftlogs_app, ["auth", "status", "--no-live"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["auth"]["public_api_access"]["ready"] is True
    assert payload["auth"]["public_api_access"]["validation"] == "skipped"
    assert payload["auth"]["public_api_access"]["live_validated"] is False
    assert payload["auth"]["user_api_access"]["ready"] is True
    assert payload["auth"]["user_api_access"]["validation"] == "skipped"
    assert payload["auth"]["user_api_access"]["live_validated"] is False


def test_warcraftlogs_auth_status_live_probe_uses_uncached_user_helper(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": False, "env_file": None})(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": True,
            "readable": True,
            "valid_json": True,
            "auth_mode": "pkce",
            "has_access_token": True,
            "has_refresh_token": True,
            "expires_at": 1500.0,
            "expired": False,
        },
    )

    class _ProbeAwareClient(_FakeWarcraftLogsClient):
        def current_user(self) -> dict[str, object]:
            raise AssertionError("auth status should use the uncached user probe helper")

        def probe_live_user_api(self) -> dict[str, object]:
            return {
                "id": 55,
                "name": "Auro",
                "avatar": "https://assets.example/avatar.png",
            }

    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _ProbeAwareClient)

    result = runner.invoke(warcraftlogs_app, ["auth", "status"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["auth"]["user_api_access"]["ready"] is True
    assert payload["auth"]["user_api_access"]["validation"] == "live"
    assert payload["auth"]["user_api_access"]["live_validated"] is True


def test_warcraftlogs_auth_status_reports_live_user_auth_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": False, "env_file": None})(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": True,
            "readable": True,
            "valid_json": True,
            "auth_mode": "pkce",
            "has_access_token": True,
            "has_refresh_token": True,
            "expires_at": 1500.0,
            "expired": False,
        },
    )

    class _AuthFailingUserClient(_FakeWarcraftLogsClient):
        def current_user(self) -> dict[str, object]:
            raise WarcraftLogsClientError("auth_failed", "Saved Warcraft Logs user token was rejected by Warcraft Logs.")

    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _AuthFailingUserClient)

    result = runner.invoke(warcraftlogs_app, ["auth", "status"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["auth"]["user_api_access"]["ready"] is False
    assert payload["auth"]["user_api_access"]["reason"] == "auth_failed"
    assert payload["auth"]["user_api_access"]["validation"] == "live"


def test_warcraftlogs_auth_status_reports_invalid_runtime_config(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": True, "env_file": "/tmp/.env.local"})(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": True,
            "readable": True,
            "valid_json": True,
            "auth_mode": "pkce",
            "has_access_token": True,
            "has_refresh_token": True,
            "expires_at": 1500.0,
            "expired": False,
        },
    )

    class _BrokenClient:
        def __init__(self) -> None:
            raise ValueError("WOWHEAD_REDIS_URL is required when WOWHEAD_CACHE_BACKEND=redis.")

    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _BrokenClient)

    result = runner.invoke(warcraftlogs_app, ["auth", "status"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["auth"]["runtime_access"]["ready"] is False
    assert "WARCRAFTLOGS_REDIS_URL" in payload["auth"]["runtime_access"]["message"]
    assert "WOWHEAD_REDIS_URL" not in payload["auth"]["runtime_access"]["message"]
    assert payload["auth"]["public_api_access"]["ready"] is False
    assert payload["auth"]["public_api_access"]["reason"] == "invalid_runtime_config"
    assert payload["auth"]["user_api_access"]["ready"] is False
    assert payload["auth"]["user_api_access"]["reason"] == "invalid_runtime_config"
    assert payload["auth"]["grants"]["client_credentials"] == "invalid_runtime_config"
    assert payload["auth"]["grants"]["authorization_code"] == "invalid_runtime_config"
    assert payload["auth"]["grants"]["pkce"] == "invalid_runtime_config"


def test_warcraftlogs_auth_status_prioritizes_invalid_runtime_config_without_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": False, "env_file": None})(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": False,
            "readable": False,
            "valid_json": False,
            "auth_mode": None,
            "has_access_token": False,
            "has_refresh_token": False,
            "expires_at": None,
            "expired": None,
        },
    )

    class _BrokenClient:
        def __init__(self) -> None:
            raise ValueError("WOWHEAD_REDIS_URL is required when WOWHEAD_CACHE_BACKEND=redis.")

    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _BrokenClient)

    result = runner.invoke(warcraftlogs_app, ["auth", "status"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["auth"]["public_api_access"]["reason"] == "invalid_runtime_config"
    assert payload["auth"]["user_api_access"]["reason"] == "invalid_runtime_config"
    assert payload["auth"]["grants"]["client_credentials"] == "invalid_runtime_config"
    assert payload["auth"]["grants"]["authorization_code"] == "invalid_runtime_config"
    assert payload["auth"]["grants"]["pkce"] == "invalid_runtime_config"


def test_warcraftlogs_auth_client_reports_endpoint_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": True, "env_file": "/tmp/.env.local", "client_id": "1234567890abcdef"})(),
    )

    result = runner.invoke(warcraftlogs_app, ["auth", "client"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["client"]["configured"] is True
    assert payload["client"]["client_id"] == "12345678..."
    assert payload["client"]["client_api_url"].endswith("/api/v2/client")
    assert payload["client"]["user_api_url"].endswith("/api/v2/user")


def test_warcraftlogs_auth_token_reports_state_summary(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.main.provider_auth_status",
        lambda provider: {
            "path": "/tmp/state/warcraftlogs.json",
            "exists": True,
            "readable": True,
            "valid_json": True,
            "auth_mode": "pkce",
            "pending_auth_mode": None,
            "has_pending_state": False,
            "has_access_token": True,
            "has_refresh_token": True,
            "expires_at": 1500.0,
            "expired": False,
        },
    )

    result = runner.invoke(warcraftlogs_app, ["auth", "token"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["token"]["active_mode"] == "pkce"
    assert payload["token"]["endpoint_family"] == "user"
    assert payload["token"]["state"]["has_refresh_token"] is True


def test_warcraftlogs_auth_login_generates_authorize_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())
    monkeypatch.setattr("warcraftlogs_cli.main._random_state_token", lambda: "pending-state-123")

    result = runner.invoke(
        warcraftlogs_app,
        ["auth", "login", "--redirect-uri", "http://127.0.0.1:8787/callback"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["mode"] == "authorization_code"
    assert payload["step"] == "authorize"
    assert payload["state"] == "pending-state-123"
    assert "oauth/authorize" in payload["authorize_url"]


def test_warcraftlogs_auth_login_can_request_scope(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())
    monkeypatch.setattr("warcraftlogs_cli.main._random_state_token", lambda: "pending-state-123")

    result = runner.invoke(
        warcraftlogs_app,
        [
            "auth",
            "login",
            "--redirect-uri",
            "http://127.0.0.1:8787/callback",
            "--scope",
            "view-user-profile",
        ],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["requested_scopes"] == ["view-user-profile"]
    assert "scope=view-user-profile" in payload["authorize_url"]


def test_warcraftlogs_auth_login_exchanges_code(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    state_file = tmp_path / "state-home" / "warcraft" / "providers" / "warcraftlogs.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps(
            {
                "pending_auth_mode": "authorization_code",
                "pending_state": "pending-state-123",
                "redirect_uri": "http://127.0.0.1:8787/callback",
            }
        )
    )
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())
    monkeypatch.setattr("warcraftlogs_cli.main.time.time", lambda: 1000.0)

    result = runner.invoke(
        warcraftlogs_app,
        [
            "auth",
            "login",
            "--redirect-uri",
            "http://127.0.0.1:8787/callback",
            "--code",
            "code-123",
            "--state",
            "pending-state-123",
        ],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["mode"] == "authorization_code"
    assert payload["step"] == "token_exchanged"
    assert payload["endpoint_family"] == "user"
    assert payload["token"]["expires_at"] == 4600.0

    saved_state = json.loads(state_file.read_text())
    assert saved_state["auth_mode"] == "authorization_code"
    assert saved_state["access_token"] == "user-token"


def test_warcraftlogs_auth_pkce_login_generates_authorize_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())
    monkeypatch.setattr("warcraftlogs_cli.main._random_state_token", lambda: "pending-state-456")
    monkeypatch.setattr("warcraftlogs_cli.main._pkce_verifier", lambda: "verifier-123")
    monkeypatch.setattr("warcraftlogs_cli.main._pkce_challenge", lambda verifier: "challenge-123")

    result = runner.invoke(
        warcraftlogs_app,
        ["auth", "pkce-login", "--redirect-uri", "http://127.0.0.1:8787/callback"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["mode"] == "pkce"
    assert payload["step"] == "authorize"
    assert payload["state"] == "pending-state-456"
    assert "challenge-123" in payload["authorize_url"]


def test_warcraftlogs_auth_whoami_uses_user_endpoint_client(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(warcraftlogs_app, ["auth", "whoami"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["endpoint_family"] == "user"
    assert payload["user"]["name"] == "Auro"


def test_warcraftlogs_auth_whoami_requires_saved_user_token_not_client_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_warcraftlogs_auth_config",
        lambda start_dir=None: type(
            "Auth",
            (),
            {
                "configured": False,
                "client_id": None,
                "client_secret": None,
                "env_file": None,
            },
        )(),
    )
    monkeypatch.setattr("warcraftlogs_cli.client.load_provider_auth_state", lambda provider: None)

    result = runner.invoke(warcraftlogs_app, ["auth", "whoami"])
    assert result.exit_code == 1

    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_user_auth"


def test_warcraftlogs_auth_whoami_reports_invalid_runtime_config_cleanly(monkeypatch) -> None:
    class _BrokenClient:
        def __init__(self) -> None:
            raise ValueError("WOWHEAD_REDIS_URL is required when WOWHEAD_CACHE_BACKEND=redis.")

    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _BrokenClient)

    result = runner.invoke(warcraftlogs_app, ["auth", "whoami"])
    assert result.exit_code == 1

    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_runtime_config"
    assert "WARCRAFTLOGS_REDIS_URL" in payload["error"]["message"]
    assert "WOWHEAD_REDIS_URL" not in payload["error"]["message"]


def test_warcraftlogs_auth_login_requires_client_credentials_cleanly(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_warcraftlogs_auth_config",
        lambda start_dir=None: type(
            "Auth",
            (),
            {
                "configured": False,
                "client_id": None,
                "client_secret": None,
                "env_file": None,
            },
        )(),
    )

    result = runner.invoke(
        warcraftlogs_app,
        ["auth", "login", "--redirect-uri", "http://127.0.0.1:8787/callback"],
    )
    assert result.exit_code == 1

    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_client_credentials"
    state_file = tmp_path / "state-home" / "warcraft" / "providers" / "warcraftlogs.json"
    assert not state_file.exists()


def test_warcraftlogs_auth_logout_removes_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    state_file = tmp_path / "state-home" / "warcraft" / "providers" / "warcraftlogs.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({"auth_mode": "authorization_code", "access_token": "token"}))

    result = runner.invoke(warcraftlogs_app, ["auth", "logout"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["auth"]["removed"] is True
    assert not state_file.exists()


def test_warcraftlogs_rate_limit_and_world_metadata_commands(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    rate_limit_result = runner.invoke(warcraftlogs_app, ["rate-limit"])
    assert rate_limit_result.exit_code == 0
    rate_limit_payload = json.loads(rate_limit_result.stdout)
    assert rate_limit_payload["rate_limit"]["limit_per_hour"] == 3600

    regions_result = runner.invoke(warcraftlogs_app, ["regions"])
    assert regions_result.exit_code == 0
    regions_payload = json.loads(regions_result.stdout)
    assert regions_payload["count"] == 2
    assert regions_payload["regions"][0]["slug"] == "us"

    expansions_result = runner.invoke(warcraftlogs_app, ["expansions"])
    assert expansions_result.exit_code == 0
    expansions_payload = json.loads(expansions_result.stdout)
    assert expansions_payload["count"] == 1
    assert expansions_payload["expansions"][0]["zone_count"] == 1

    server_result = runner.invoke(warcraftlogs_app, ["server", "us", "illidan"])
    assert server_result.exit_code == 0
    server_payload = json.loads(server_result.stdout)
    assert server_payload["server"]["slug"] == "illidan"

    zones_result = runner.invoke(warcraftlogs_app, ["zones", "--expansion-id", "12"])
    assert zones_result.exit_code == 0
    zones_payload = json.loads(zones_result.stdout)
    assert zones_payload["count"] == 1
    assert zones_payload["zones"][0]["encounters"][0]["journal_id"] == 9001

    encounter_result = runner.invoke(warcraftlogs_app, ["encounter", "3012"])
    assert encounter_result.exit_code == 0
    encounter_payload = json.loads(encounter_result.stdout)
    assert encounter_payload["encounter"]["zone"]["expansion"]["name"] == "Midnight"
    assert encounter_payload["encounter_identity"]["status"] == "canonical"
    assert encounter_payload["encounter_identity"]["identity"]["journal_id"] == 9001

    zone_result = runner.invoke(warcraftlogs_app, ["zone", "38"])
    assert zone_result.exit_code == 0
    zone_payload = json.loads(zone_result.stdout)
    assert zone_payload["zone"]["partitions"][0]["default"] is True


def test_warcraftlogs_guild_character_and_report_commands(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    guild_result = runner.invoke(warcraftlogs_app, ["guild", "us", "illidan", "Liquid", "--zone-id", "38"])
    assert guild_result.exit_code == 0
    guild_payload = json.loads(guild_result.stdout)
    assert guild_payload["guild"]["zone_ranking"]["progress"]["world"]["number"] == 2

    character_result = runner.invoke(warcraftlogs_app, ["character", "us", "illidan", "Roguecane"])
    assert character_result.exit_code == 0
    character_payload = json.loads(character_result.stdout)
    assert character_payload["character"]["server"]["slug"] == "illidan"
    assert character_payload["character"]["guild_rank"] == 3
    assert character_payload["character"]["guilds"][0]["name"] == "Liquid"

    guild_rankings_result = runner.invoke(
        warcraftlogs_app,
        ["guild-rankings", "us", "illidan", "Liquid", "--zone-id", "38", "--size", "20", "--difficulty", "5"],
    )
    assert guild_rankings_result.exit_code == 0
    guild_rankings_payload = json.loads(guild_rankings_result.stdout)
    assert guild_rankings_payload["guild_rankings"]["zone_ranking"]["speed"]["world"]["number"] == 4

    guild_members_result = runner.invoke(
        warcraftlogs_app,
        ["guild-members", "us", "illidan", "Liquid", "--limit", "2", "--page", "1"],
    )
    assert guild_members_result.exit_code == 0
    guild_members_payload = json.loads(guild_members_result.stdout)
    assert guild_members_payload["guild_members"]["pagination"]["total"] == 1
    assert guild_members_payload["guild_members"]["members"][0]["name"] == "Roguecane"
    assert guild_members_payload["notes"] == ["Guild roster queries only work for games where Warcraft Logs can verify guild membership."]

    guild_attendance_result = runner.invoke(
        warcraftlogs_app,
        ["guild-attendance", "us", "illidan", "Liquid", "--guild-tag-id", "5", "--limit", "2", "--page", "1", "--zone-id", "38"],
    )
    assert guild_attendance_result.exit_code == 0
    guild_attendance_payload = json.loads(guild_attendance_result.stdout)
    assert guild_attendance_payload["guild_attendance"]["pagination"]["total"] == 1
    assert guild_attendance_payload["guild_attendance"]["attendance"][0]["player_count"] == 2
    assert guild_attendance_payload["guild_attendance"]["attendance"][0]["players"][0]["presence_label"] == "present"
    assert guild_attendance_payload["guild_attendance"]["attendance"][0]["players"][1]["presence_label"] == "benched"

    guild_reports_result = runner.invoke(
        warcraftlogs_app,
        [
            "guild-reports",
            "us",
            "illidan",
            "Liquid",
            "--limit",
            "10",
            "--page",
            "2",
            "--start-time",
            "1000",
            "--end-time",
            "2000",
            "--zone-id",
            "38",
            "--game-zone-id",
            "12961",
        ],
    )
    assert guild_reports_result.exit_code == 0
    guild_reports_payload = json.loads(guild_reports_result.stdout)
    assert guild_reports_payload["guild"]["name"] == "Liquid"
    assert guild_reports_payload["count"] == 1
    assert guild_reports_payload["reports"][0]["code"] == "abcd1234"

    character_rankings_result = runner.invoke(
        warcraftlogs_app,
        [
            "character-rankings",
            "us",
            "illidan",
            "Roguecane",
            "--zone-id",
            "38",
            "--difficulty",
            "5",
            "--metric",
            "dps",
            "--size",
            "20",
            "--spec-name",
            "assassination",
        ],
    )
    assert character_rankings_result.exit_code == 0
    character_rankings_payload = json.loads(character_rankings_result.stdout)
    assert character_rankings_payload["character_rankings"]["summary"]["best_performance_average"] == 85.4
    assert character_rankings_payload["character_rankings"]["rankings"][0]["encounter"]["name"] == "Dimensius"

    report_result = runner.invoke(warcraftlogs_app, ["report", "abcd1234", "--allow-unlisted"])
    assert report_result.exit_code == 0
    report_payload = json.loads(report_result.stdout)
    assert report_payload["report"]["zone"]["name"] == "Manaforge Omega"
    assert report_payload["report"]["archive_status"]["is_archived"] is True

    reports_result = runner.invoke(
        warcraftlogs_app,
        [
            "reports",
            "--guild-region",
            "us",
            "--guild-realm",
            "illidan",
            "--guild-name",
            "Liquid",
            "--limit",
            "10",
            "--page",
            "2",
            "--start-time",
            "1000",
            "--end-time",
            "2000",
            "--zone-id",
            "38",
            "--game-zone-id",
            "12961",
        ],
    )
    assert reports_result.exit_code == 0
    reports_payload = json.loads(reports_result.stdout)
    assert reports_payload["pagination"]["current_page"] == 2
    assert reports_payload["count"] == 1
    assert reports_payload["reports"][0]["archive_status"]["archive_date"] == 789

    fights_result = runner.invoke(warcraftlogs_app, ["report-fights", "abcd1234", "--difficulty", "5"])
    assert fights_result.exit_code == 0
    fights_payload = json.loads(fights_result.stdout)
    assert fights_payload["count"] == 3
    assert fights_payload["fights"][0]["encounter_id"] == 3012

    events_result = runner.invoke(
        warcraftlogs_app,
        [
            "report-events",
            "abcd1234",
            "--data-type",
            "casts",
            "--difficulty",
            "5",
            "--encounter-id",
            "3012",
            "--fight-id",
            "1",
            "--fight-id",
            "2",
            "--limit",
            "50",
            "--source-id",
            "9",
        ],
    )
    assert events_result.exit_code == 0
    events_payload = json.loads(events_result.stdout)
    assert events_payload["next_page_timestamp"] == 999.0
    assert events_payload["events"][0]["type"] == "cast"

    table_result = runner.invoke(
        warcraftlogs_app,
        ["report-table", "abcd1234", "--allow-unlisted", "--data-type", "damage-done", "--view-by", "source"],
    )
    assert table_result.exit_code == 0
    table_payload = json.loads(table_result.stdout)
    assert table_payload["table"]["entries"][0]["name"] == "Auropower"

    graph_result = runner.invoke(
        warcraftlogs_app,
        ["report-graph", "abcd1234", "--data-type", "damage-done", "--view-by", "target"],
    )
    assert graph_result.exit_code == 0
    graph_payload = json.loads(graph_result.stdout)
    assert graph_payload["graph"]["series"][0]["name"] == "Damage"

    master_data_result = runner.invoke(
        warcraftlogs_app,
        ["report-master-data", "abcd1234", "--no-translate", "--actor-type", "Player", "--actor-sub-type", "Paladin"],
    )
    assert master_data_result.exit_code == 0
    master_data_payload = json.loads(master_data_result.stdout)
    assert master_data_payload["master_data"]["log_version"] == 47
    assert master_data_payload["master_data"]["actors"][0]["name"] == "Auropower"
    assert master_data_payload["master_data"]["actors"][0]["identity_contract"]["status"] == "normalized"
    assert master_data_payload["master_data"]["abilities"][0]["identity_contract"]["status"] == "canonical"

    player_details_result = runner.invoke(
        warcraftlogs_app,
        [
            "report-player-details",
            "abcd1234",
            "--difficulty",
            "5",
            "--encounter-id",
            "3012",
            "--fight-id",
            "1",
            "--fight-id",
            "2",
            "--include-combatant-info",
            "--kill-type",
            "kills",
        ],
    )
    assert player_details_result.exit_code == 0
    player_details_payload = json.loads(player_details_result.stdout)
    assert player_details_payload["player_details"]["counts"]["total"] == 2
    assert player_details_payload["player_details"]["roles"]["tanks"][0]["name"] == "Sherway"
    assert player_details_payload["player_details"]["roles"]["tanks"][0]["identity_contract"]["status"] == "normalized"

    rankings_result = runner.invoke(
        warcraftlogs_app,
        [
            "report-rankings",
            "abcd1234",
            "--allow-unlisted",
            "--compare",
            "rankings",
            "--difficulty",
            "5",
            "--encounter-id",
            "3012",
            "--fight-id",
            "1",
            "--fight-id",
            "2",
            "--player-metric",
            "dps",
            "--timeframe",
            "historical",
        ],
    )
    assert rankings_result.exit_code == 0
    rankings_payload = json.loads(rankings_result.stdout)
    assert rankings_payload["rankings"]["count"] == 1
    assert rankings_payload["rankings"]["rows"][0]["name"] == "Auropower"


def test_warcraftlogs_boss_kills_samples_finished_reports_and_filters_by_spec(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        [
            "boss-kills",
            "--zone-id",
            "38",
            "--boss-id",
            "3012",
            "--difficulty",
            "5",
            "--spec-name",
            "Retribution",
            "--kill-time-max",
            "150",
            "--top",
            "5",
            "--report-pages",
            "1",
            "--reports-per-page",
            "10",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "boss_kills"
    assert payload["ranking_basis"] == "sampled_fastest_kills"
    assert "not a global spec ranking leaderboard" in payload["notes"][0]
    assert payload["sample"]["source_report_count"] == 2
    assert payload["sample"]["finished_report_count"] == 1
    assert payload["sample"]["skipped_live_report_count"] == 1
    assert payload["sample"]["filtered_kill_count"] == 1
    assert payload["kills"][0]["fight"]["encounter_id"] == 3012
    assert payload["kills"][0]["duration_seconds"] == 100.0
    assert payload["kills"][0]["matching_players"][0]["name"] == "Auropower"


def test_warcraftlogs_top_kills_reports_truncation(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        [
            "top-kills",
            "--zone-id",
            "38",
            "--boss-id",
            "3012",
            "--difficulty",
            "5",
            "--top",
            "1",
            "--report-pages",
            "1",
            "--reports-per-page",
            "10",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "top_kills"
    assert payload["count"] == 1
    assert payload["sample"]["truncated"] is False
    assert payload["kills"][0]["fight"]["name"] == "Dimensius, the All-Devouring"


def test_warcraftlogs_kill_time_distribution_returns_histogram(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        [
            "kill-time-distribution",
            "--zone-id",
            "38",
            "--boss-name",
            "Dimensius",
            "--difficulty",
            "5",
            "--report-pages",
            "1",
            "--reports-per-page",
            "10",
            "--bucket-seconds",
            "30",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "kill_time_distribution"
    assert payload["sample"]["filtered_kill_count"] == 1
    assert payload["distribution"]["statistics"]["min"] == 100.0
    assert payload["distribution"]["rows"][0]["start_seconds"] == 90


def test_warcraftlogs_boss_spec_usage_returns_sorted_spec_rows(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        [
            "boss-spec-usage",
            "--zone-id",
            "38",
            "--boss-id",
            "3012",
            "--difficulty",
            "5",
            "--top",
            "5",
            "--report-pages",
            "1",
            "--reports-per-page",
            "10",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "boss_spec_usage"
    assert payload["ranking_basis"] == "sampled_finished_kill_cohort_spec_presence"
    assert payload["sample"]["filtered_kill_count"] == 1
    assert payload["sample"]["sampled_player_row_count"] == 2
    assert payload["spec_usage"][0]["spec_name"] == "Protection"
    assert payload["spec_usage"][0]["role"] == "tanks"
    assert payload["spec_usage"][0]["kill_presence_count"] == 1
    assert payload["spec_usage"][0]["percent_of_kills"] == 100.0


def test_warcraftlogs_encounter_rankings_returns_real_ranking_rows(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        [
            "encounter-rankings",
            "--zone-id",
            "38",
            "--boss-name",
            "Dimensius",
            "--bracket",
            "1",
            "--difficulty",
            "5",
            "--class-name",
            "Druid",
            "--spec-name",
            "Balance",
            "--metric",
            "dps",
            "--page",
            "1",
            "--partition",
            "2",
            "--size",
            "20",
            "--server-region",
            "us",
            "--server-slug",
            "illidan",
            "--leaderboard",
            "logs-only",
            "--hard-mode-level",
            "no-hard-mode",
            "--filter",
            "duration>120",
            "--include-combatant-info",
            "--include-other-players",
            "--top",
            "1",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "encounter_rankings"
    assert payload["ranking_basis"] == "encounter_character_rankings"
    assert payload["query"]["boss_id"] == 3012
    assert payload["query"]["class_name"] == "Druid"
    assert payload["query"]["spec_name"] == "Balance"
    assert payload["query"]["leaderboard"] == "LogsOnly"
    assert payload["query"]["hard_mode_level"] == "NormalMode"
    assert payload["encounter"]["name"] == "Dimensius, the All-Devouring"
    assert payload["rankings"]["count"] == 1
    assert payload["rankings"]["page_count"] == 2
    assert payload["rankings"]["truncated"] is True
    assert payload["rankings"]["has_more_pages"] is True
    assert payload["rankings"]["rows"][0]["name"] == "Moonkinone"
    assert payload["rankings"]["rows"][0]["rank"] == 1
    assert payload["rankings"]["rows"][0]["out_of"] is None
    assert payload["rankings"]["rows"][0]["rank_percent"] is None
    assert payload["rankings"]["rows"][0]["server_name"] == "Illidan"
    assert payload["rankings"]["rows"][0]["server_region"] == "US"
    assert payload["rankings"]["rows"][0]["guild_name"] == "Liquid"
    assert payload["rankings"]["rows"][0]["start_time"] == 111
    assert payload["rankings"]["rows"][0]["fight_id"] == 1
    assert payload["rankings"]["rows"][0]["report_url"] == "https://www.warcraftlogs.com/reports/abcd1234#fight=1"
    assert payload["rankings"]["rows"][0]["has_combatant_info"] is True
    assert payload["rankings"]["rows"][0]["other_players_count"] == 2
    assert payload["rankings"]["rows"][0]["class_spec_identity"]["identity"] == {"actor_class": "druid", "spec": "balance"}


def test_warcraftlogs_encounter_rankings_normalizes_slug_filters(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        [
            "encounter-rankings",
            "--zone-id",
            "38",
            "--boss-name",
            "Dimensius",
            "--bracket",
            "1",
            "--difficulty",
            "5",
            "--class-name",
            "druid",
            "--spec-name",
            "balance",
            "--metric",
            "dps",
            "--page",
            "1",
            "--partition",
            "2",
            "--size",
            "20",
            "--server-region",
            "us",
            "--server-slug",
            "illidan",
            "--leaderboard",
            "logs-only",
            "--hard-mode-level",
            "no-hard-mode",
            "--filter",
            "duration>120",
            "--include-combatant-info",
            "--include-other-players",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"]["class_name"] == "druid"
    assert payload["query"]["spec_name"] == "balance"
    assert payload["rankings"]["rows"][0]["class_name"] == "Druid"
    assert payload["rankings"]["rows"][0]["spec_name"] == "Balance"


def test_warcraftlogs_encounter_rankings_derives_page_offset_ranks(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        [
            "encounter-rankings",
            "--zone-id",
            "38",
            "--boss-name",
            "Dimensius",
            "--bracket",
            "1",
            "--difficulty",
            "5",
            "--class-name",
            "Druid",
            "--spec-name",
            "Balance",
            "--metric",
            "dps",
            "--page",
            "2",
            "--partition",
            "2",
            "--size",
            "20",
            "--server-region",
            "us",
            "--server-slug",
            "illidan",
            "--leaderboard",
            "logs-only",
            "--hard-mode-level",
            "no-hard-mode",
            "--filter",
            "duration>120",
            "--include-combatant-info",
            "--include-other-players",
            "--top",
            "1",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["rankings"]["page"] == 2
    assert payload["rankings"]["page_count"] == 1
    assert payload["rankings"]["has_more_pages"] is False
    assert payload["rankings"]["rows"][0]["name"] == "Moonkinthree"
    assert payload["rankings"]["rows"][0]["rank"] == 101


def test_warcraftlogs_encounter_rankings_surfaces_embedded_provider_errors(monkeypatch) -> None:
    class _RankingErrorClient(_FakeWarcraftLogsClient):
        def encounter_rankings(self, *, encounter_id: int, options: object) -> dict[str, object]:
            assert encounter_id == 3012
            assert options.class_name == "Hunter"
            assert options.spec_name == "Marksmanship"
            return {
                "id": 3012,
                "name": "Dimensius, the All-Devouring",
                "journalID": 9001,
                "zone": {"id": 38, "name": "Manaforge Omega", "expansion": {"id": 12, "name": "Midnight"}},
                "characterRankings": {"error": "Invalid class and spec specified."},
            }

    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _RankingErrorClient())

    result = runner.invoke(
        warcraftlogs_app,
        [
            "encounter-rankings",
            "--zone-id",
            "38",
            "--boss-name",
            "Dimensius",
            "--class-name",
            "Hunter",
            "--spec-name",
            "Marksmanship",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_query"
    assert payload["error"]["message"] == "Invalid class and spec specified."


def test_warcraftlogs_encounter_rankings_accepts_matching_boss_id_and_name(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        [
            "encounter-rankings",
            "--zone-id",
            "38",
            "--boss-id",
            "3012",
            "--boss-name",
            "Dimensius",
            "--bracket",
            "1",
            "--difficulty",
            "5",
            "--class-name",
            "Druid",
            "--spec-name",
            "Balance",
            "--metric",
            "dps",
            "--page",
            "1",
            "--partition",
            "2",
            "--size",
            "20",
            "--server-region",
            "us",
            "--server-slug",
            "illidan",
            "--leaderboard",
            "logs-only",
            "--hard-mode-level",
            "no-hard-mode",
            "--filter",
            "duration>120",
            "--include-combatant-info",
            "--include-other-players",
            "--top",
            "1",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"]["boss_id"] == 3012
    assert payload["encounter"]["name"] == "Dimensius, the All-Devouring"


def test_warcraftlogs_ability_usage_summary_returns_sampled_cast_summary(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        [
            "ability-usage-summary",
            "--zone-id",
            "38",
            "--boss-id",
            "3012",
            "--difficulty",
            "5",
            "--ability-id",
            "20473",
            "--preview-limit",
            "5",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "ability_usage_summary"
    assert payload["query"]["ability_id"] == 20473
    assert payload["freshness"]["sampled_at"].endswith("Z")
    assert payload["freshness"]["cache_ttl_seconds"] is None
    assert payload["citations"]["sample_reports"] == [
        {
            "report_code": "abcd1234",
            "fight_id": 1,
            "report_url": "https://www.warcraftlogs.com/reports/abcd1234#fight=1",
        }
    ]
    assert payload["ability"]["game_id"] == 20473
    assert payload["ability"]["name"] == "Holy Shock"
    assert payload["usage"]["total_casts"] == 2
    assert payload["usage"]["kills_with_any_usage_count"] == 1
    assert payload["usage"]["kills_with_any_usage_percent"] == 100.0
    assert payload["kills_preview"][0]["casts"]["count"] == 2
    assert payload["kills_preview"][0]["casts"]["sources"][0]["count"] == 2
    assert payload["kills_preview"][0]["casts"]["sources"][0]["source"]["name"] == "Auropower"


def test_warcraftlogs_comp_samples_returns_sampled_rosters_and_class_presence(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        [
            "comp-samples",
            "--zone-id",
            "38",
            "--boss-id",
            "3012",
            "--difficulty",
            "5",
            "--top",
            "5",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "comp_samples"
    assert payload["freshness"]["sampled_at"].endswith("Z")
    assert payload["freshness"]["cache_ttl_seconds"] is None
    assert payload["citations"]["sample_reports"][0]["report_url"] == "https://www.warcraftlogs.com/reports/abcd1234#fight=1"
    assert payload["sample"]["filtered_kill_count"] == 1
    assert payload["sample"]["sampled_player_count"] == 2
    assert payload["class_presence"][0]["class_name"] == "Paladin"
    assert payload["class_presence"][0]["kill_presence_count"] == 1
    assert payload["composition_signatures"][0]["class_signature"] == "Paladinx1|Warriorx1"
    assert payload["kills"][0]["composition"]["role_counts"]["dps"] == 1
    assert payload["kills"][0]["composition"]["role_counts"]["tanks"] == 1
    assert payload["kills"][0]["player_details"]["players"][0]["identity_contract"]["status"] == "canonical"


def test_warcraftlogs_cross_report_commands_require_boss_scope(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        ["boss-kills", "--zone-id", "38"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_boss"


def test_warcraftlogs_report_encounter_accepts_report_url(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        ["report-encounter", "https://www.warcraftlogs.com/reports/abcd1234#fight=1"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "report_encounter"
    assert payload["reference"]["code"] == "abcd1234"
    assert payload["reference"]["fight_id"] == 1
    assert payload["fight"]["encounter_id"] == 3012
    assert payload["encounter_identity"]["status"] == "canonical"
    assert payload["encounter_identity"]["identity"]["encounter_id"] == 3012
    assert payload["stability"]["cache_safe"] is True


def test_warcraftlogs_report_encounter_requires_explicit_fight_scope(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(warcraftlogs_app, ["report-encounter", "abcd1234"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_scope"


def test_warcraftlogs_report_encounter_players_scopes_to_selected_fight(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        ["report-encounter-players", "abcd1234", "--fight-id", "1", "--include-combatant-info"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "report_encounter_players"
    assert payload["reference"]["fight_id"] == 1
    assert payload["player_details"]["counts"]["total"] == 2
    assert payload["player_details"]["roles"]["dps"][0]["name"] == "Auropower"
    assert payload["player_details"]["roles"]["dps"][0]["identity_contract"]["status"] == "canonical"
    assert payload["player_details"]["roles"]["dps"][0]["class_spec_identity"]["identity"]["spec"] == "retribution"


def test_warcraftlogs_report_player_talents_emits_raw_transport_packet(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "simc_trait_resolution_incomplete",
            },
        },
    )

    result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "report_player_talents"
    assert payload["reference"]["fight_id"] == 1
    assert payload["player"]["id"] == 9
    assert payload["talent_transport_packet"]["kind"] == "talent_transport_packet"
    assert payload["talent_transport_packet"]["transport_status"] == "raw_only"
    assert payload["talent_transport_packet"]["build_identity"]["class_spec_identity"]["identity"] == {
        "actor_class": "paladin",
        "spec": "retribution",
    }
    assert payload["talent_transport_packet"]["raw_evidence"]["talent_tree_entries"][0] == {
        "entry": 103324,
        "node_id": 82244,
        "rank": 1,
    }
    assert payload["talent_transport_packet"]["validation"]["status"] == "not_validated"


def test_warcraftlogs_report_player_talents_passes_allow_unlisted(monkeypatch) -> None:
    class _AllowUnlistedClient(_FakeWarcraftLogsClient):
        def report(self, *, code: str, allow_unlisted: bool = False) -> dict[str, object]:
            assert allow_unlisted is True
            return super().report(code=code, allow_unlisted=allow_unlisted)

        def report_fights(
            self,
            *,
            code: str,
            difficulty: int | None = None,
            allow_unlisted: bool = False,
            ttl_override: int | None = None,
        ) -> dict[str, object]:
            assert allow_unlisted is True
            return super().report_fights(
                code=code,
                difficulty=difficulty,
                allow_unlisted=False,
                ttl_override=ttl_override,
            )

        def report_player_details(self, *, code: str, allow_unlisted: bool = False, options, ttl_override: int | None = None) -> dict[str, object]:  # noqa: ANN001
            assert allow_unlisted is True
            return super().report_player_details(
                code=code,
                allow_unlisted=False,
                options=options,
                ttl_override=ttl_override,
            )

    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _AllowUnlistedClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "simc_trait_resolution_incomplete",
            },
        },
    )

    result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9", "--allow-unlisted"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["talent_transport_packet"]["transport_status"] == "raw_only"


def test_warcraftlogs_report_player_talents_rejects_missing_actor(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "999"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["message"] == "Actor ID 999 was not present in the selected fight."


def test_warcraftlogs_report_player_talents_rejects_null_only_talent_rows(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main._player_detail_actor",
        lambda details_payload, actor_id: {
            "id": actor_id,
            "combatant_info": {
                "talentTree": [
                    {"id": None, "nodeID": None, "rank": None},
                    {"name": "bad row"},
                ]
            },
            "class_spec_identity": {
                "identity": {"actor_class": "paladin", "spec": "retribution"},
            },
        },
    )

    result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_talent_tree"


def test_warcraftlogs_report_player_talents_rejects_mixed_valid_and_invalid_talent_rows(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main._player_detail_actor",
        lambda details_payload, actor_id: {
            "id": actor_id,
            "combatant_info": {
                "talentTree": [
                    {"id": 103324, "nodeID": 82244, "rank": 1},
                    {"id": None, "nodeID": None, "rank": 1},
                ]
            },
            "class_spec_identity": {
                "identity": {"actor_class": "paladin", "spec": "retribution"},
            },
        },
    )

    result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_talent_tree"
    assert "incomplete combatant_info.talentTree rows" in payload["error"]["message"]


def test_warcraftlogs_report_player_talents_rejects_non_dict_talent_rows(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main._player_detail_actor",
        lambda details_payload, actor_id: {
            "id": actor_id,
            "combatant_info": {
                "talentTree": [
                    {"id": 103324, "nodeID": 82244, "rank": 1},
                    "bad-row",
                ]
            },
            "class_spec_identity": {
                "identity": {"actor_class": "paladin", "spec": "retribution"},
            },
        },
    )

    result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_talent_tree"
    assert "incomplete combatant_info.talentTree rows" in payload["error"]["message"]


def test_warcraftlogs_report_player_talents_rejects_boolean_talent_rows(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main._player_detail_actor",
        lambda details_payload, actor_id: {
            "id": actor_id,
            "combatant_info": {
                "talentTree": [
                    {"id": 103324, "nodeID": 82244, "rank": 1},
                    {"id": True, "nodeID": 88206, "rank": 1},
                ]
            },
            "class_spec_identity": {
                "identity": {"actor_class": "paladin", "spec": "retribution"},
            },
        },
    )

    result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_talent_tree"
    assert "incomplete combatant_info.talentTree rows" in payload["error"]["message"]


def test_warcraftlogs_report_player_talents_rejects_incomplete_talent_rows(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main._player_detail_actor",
        lambda details_payload, actor_id: {
            "id": actor_id,
            "combatant_info": {
                "talentTree": [
                    {"id": None, "nodeID": None, "rank": 1},
                ]
            },
            "class_spec_identity": {
                "identity": {"actor_class": "paladin", "spec": "retribution"},
            },
        },
    )

    result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_talent_tree"


def test_warcraftlogs_report_player_talents_emits_validated_split_transport(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": "117176:1",
                }
            },
                "validation": {
                    "status": "validated",
                    "source": "simc_trait_data_round_trip",
                    "actor_class": "paladin",
                    "spec": "retribution",
                },
        },
    )

    result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["talent_transport_packet"]["transport_status"] == "validated"
    assert payload["talent_transport_packet"]["transport_forms"]["simc_split_talents"]["spec_talents"] == "109839:1"
    assert payload["talent_transport_packet"]["validation"]["status"] == "validated"


def test_warcraftlogs_report_player_talents_can_write_transport_packet(monkeypatch, tmp_path: Path) -> None:
    out_path = tmp_path / "gubkfc-packet.json"
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": None,
                }
            },
                "validation": {
                    "status": "validated",
                    "source": "simc_trait_data_round_trip",
                    "actor_class": "paladin",
                    "spec": "retribution",
                },
        },
    )

    result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9", "--out", str(out_path)],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["written_packet_path"] == str(out_path.resolve())

    written_packet = json.loads(out_path.read_text())
    assert written_packet["kind"] == "talent_transport_packet"
    assert written_packet["transport_status"] == "validated"
    assert written_packet["scope"] == {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9}


def test_warcraftlogs_report_player_talents_normalizes_write_failure(monkeypatch, tmp_path: Path) -> None:
    out_dir = tmp_path / "out-dir"
    out_dir.mkdir()
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                }
            },
                "validation": {
                    "status": "validated",
                    "source": "simc_trait_data_round_trip",
                    "actor_class": "paladin",
                    "spec": "retribution",
                },
        },
    )

    result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9", "--out", str(out_dir)],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "transport_packet_write_failed"


def test_warcraftlogs_report_player_talents_rejects_invalid_transport_packet(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main._player_talent_transport_packet",
        lambda *args, **kwargs: {
            "kind": "talent_transport_packet",
            "transport_status": "validated",
            "build_identity": {},
            "transport_forms": {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
            "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
            "validation": {},
            "scope": {},
        },
    )

    result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"


def test_warcraftlogs_report_encounter_casts_summarizes_cast_rows(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        ["report-encounter-casts", "abcd1234", "--fight-id", "1", "--preview-limit", "2"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "report_encounter_casts"
    assert payload["casts"]["event_count"] == 3
    assert payload["casts"]["by_source"][0]["source"]["name"] == "Auropower"
    assert payload["casts"]["by_target"][0]["target"]["name"] == "Dimensius, the All-Devouring"
    assert payload["casts"]["by_ability"][0]["ability"]["name"] == "Holy Shock"
    assert payload["casts"]["by_source_target"][0]["target"]["name"] == "Dimensius, the All-Devouring"
    assert payload["casts"]["by_source_target"][1]["target"]["name"] == "Unstable Voidling"
    assert payload["casts"]["preview"][0]["relative_time_ms"] == 20000.0
    assert payload["casts"]["preview"][0]["source"]["identity_contract"]["status"] == "canonical"
    assert payload["casts"]["preview"][0]["ability"]["identity_contract"]["status"] == "canonical"


def test_warcraftlogs_report_encounter_casts_supports_windows_and_filters(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        [
            "report-encounter-casts",
            "abcd1234",
            "--fight-id",
            "1",
            "--hostility-type",
            "friendlies",
            "--window-start-ms",
            "5000",
            "--window-end-ms",
            "30000",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "report_encounter_casts"
    assert payload["query"]["hostility_type"] == "Friendlies"
    assert payload["query"]["window_start_ms"] == 5000.0
    assert payload["query"]["window_end_ms"] == 30000.0
    assert payload["query"]["start_time"] == 105000.0
    assert payload["query"]["end_time"] == 130000.0


def test_warcraftlogs_report_encounter_window_rejects_inverted_range(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        [
            "report-encounter-casts",
            "abcd1234",
            "--fight-id",
            "1",
            "--window-start-ms",
            "30000",
            "--window-end-ms",
            "5000",
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"


def test_warcraftlogs_report_encounter_buffs_scopes_table_query(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        [
            "report-encounter-buffs",
            "abcd1234",
            "--fight-id",
            "1",
            "--window-start-ms",
            "10000",
            "--window-end-ms",
            "50000",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "report_encounter_buffs"
    assert payload["query"]["data_type"] == "Buffs"
    assert payload["query"]["start_time"] == 110000.0
    assert payload["query"]["end_time"] == 150000.0
    assert payload["table"]["entries"][0]["total"] == 98.7


def test_warcraftlogs_report_encounter_aura_summary_returns_typed_rows(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        [
            "report-encounter-aura-summary",
            "abcd1234",
            "--fight-id",
            "1",
            "--ability-id",
            "20473",
            "--window-start-ms",
            "10000",
            "--window-end-ms",
            "50000",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "report_encounter_aura_summary"
    assert payload["query"]["ability_id"] == 20473.0
    assert payload["query"]["view_by"] == "Source"
    assert payload["query"]["start_time"] == 110000.0
    assert payload["query"]["end_time"] == 150000.0
    assert payload["aura"]["name"] == "Holy Shock"
    assert payload["aura_summary"]["entry_count"] == 2
    assert payload["aura_summary"]["rows"][0]["source"]["name"] == "Auropower"
    assert payload["aura_summary"]["rows"][0]["reported_total"] == 98.7
    assert payload["aura_summary"]["rows"][0]["reported_active_time"] == 74000
    assert payload["aura_summary"]["rows"][0]["source"]["identity_contract"]["status"] == "canonical"


def test_warcraftlogs_report_encounter_aura_compare_returns_window_deltas(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        [
            "report-encounter-aura-compare",
            "abcd1234",
            "--fight-id",
            "1",
            "--ability-id",
            "20473",
            "--left-window-start-ms",
            "10000",
            "--left-window-end-ms",
            "50000",
            "--right-window-start-ms",
            "50000",
            "--right-window-end-ms",
            "90000",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "report_encounter_aura_compare"
    assert payload["windows"][0]["query"]["start_time"] == 110000.0
    assert payload["windows"][1]["query"]["start_time"] == 150000.0
    assert payload["comparison"]["matching_rule"] == "same_report_same_fight_same_ability_explicit_windows"
    auropower_row = next(row for row in payload["comparison"]["rows"] if row["source"]["name"] == "Auropower")
    assert auropower_row["left_reported_total"] == 98.7
    assert auropower_row["right_reported_total"] == 65.0
    assert auropower_row["reported_total_delta"] == -33.7


def test_warcraftlogs_report_encounter_damage_breakdown_scopes_table_query(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        ["report-encounter-damage-breakdown", "abcd1234", "--fight-id", "1"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "report_encounter_damage_breakdown"
    assert payload["query"]["data_type"] == "DamageDone"
    assert payload["query"]["fight_ids"] == [1]
    assert payload["table"]["entries"][0]["name"] == "Auropower"


def test_warcraftlogs_report_encounter_damage_source_summary_returns_typed_rows(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        ["report-encounter-damage-source-summary", "abcd1234", "--fight-id", "1"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "report_encounter_damage_source_summary"
    assert payload["query"]["view_by"] == "Source"
    assert payload["damage_summary"]["entry_count"] == 2
    assert payload["damage_summary"]["rows"][0]["source"]["name"] == "Auropower"
    assert payload["damage_summary"]["rows"][0]["reported_total"] == 123456
    assert payload["damage_summary"]["rows"][0]["source"]["identity_contract"]["status"] == "canonical"


def test_warcraftlogs_report_encounter_damage_target_summary_returns_typed_rows(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(
        warcraftlogs_app,
        ["report-encounter-damage-target-summary", "abcd1234", "--fight-id", "1"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "report_encounter_damage_target_summary"
    assert payload["query"]["view_by"] == "Target"
    assert payload["damage_summary"]["entry_count"] == 2
    assert payload["damage_summary"]["rows"][0]["target"]["name"] == "Dimensius, the All-Devouring"
    assert payload["damage_summary"]["rows"][0]["reported_total"] == 210000
    assert payload["damage_summary"]["rows"][0]["target"]["identity_contract"]["status"] == "canonical"


def test_warcraftlogs_report_events_requires_scope(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())

    result = runner.invoke(warcraftlogs_app, ["report-events", "abcd1234", "--limit", "5"])
    assert result.exit_code == 1

    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_scope"


def test_warcraftlogs_character_rankings_surfaces_provider_permission_errors(monkeypatch) -> None:
    class _PermissionClient(_FakeWarcraftLogsClient):
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
        ) -> dict[str, object]:
            return {
                "id": 99,
                "canonicalID": 99,
                "name": name,
                "classID": 6,
                "level": 90,
                "faction": {"id": 1, "name": "Alliance"},
                "server": {
                    "id": 10,
                    "name": "Illidan",
                    "normalizedName": "Illidan",
                    "slug": "illidan",
                    "region": {"id": 1, "compactName": "US", "name": "North America", "slug": "us"},
                    "subregion": {"id": 100, "name": "Chicago"},
                },
                "zoneRankings": {"error": "You do not have permission to see this character's rankings."},
            }

    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _PermissionClient())

    result = runner.invoke(
        warcraftlogs_app,
        ["character-rankings", "us", "illidan", "Driney", "--zone-id", "38", "--difficulty", "5", "--metric", "dps"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["character_rankings"]["summary"] is None
    assert payload["character_rankings"]["error"] == "You do not have permission to see this character's rankings."
    assert payload["character_rankings"]["rankings"] == []


def test_warcraftlogs_auth_prefers_local_env_before_xdg_provider_env(monkeypatch, tmp_path) -> None:
    repo_env = tmp_path / ".env.local"
    repo_env.write_text("WARCRAFTLOGS_CLIENT_ID=repo-id\nWARCRAFTLOGS_CLIENT_SECRET=repo-secret\n")
    config_home = tmp_path / "config-home"
    provider_env = config_home / "warcraft" / "providers" / "warcraftlogs.env"
    provider_env.parent.mkdir(parents=True)
    provider_env.write_text("WARCRAFTLOGS_CLIENT_ID=provider-id\nWARCRAFTLOGS_CLIENT_SECRET=provider-secret\n")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("WARCRAFTLOGS_CLIENT_ID", raising=False)
    monkeypatch.delenv("WARCRAFTLOGS_CLIENT_SECRET", raising=False)

    auth = load_warcraftlogs_auth_config(start_dir=str(tmp_path))

    assert auth.configured is True
    assert auth.client_id == "repo-id"
    assert auth.client_secret == "repo-secret"
    assert auth.env_file == str(repo_env)


def test_warcraftlogs_auth_falls_back_to_xdg_provider_env(monkeypatch, tmp_path) -> None:
    config_home = tmp_path / "config-home"
    provider_env = config_home / "warcraft" / "providers" / "warcraftlogs.env"
    provider_env.parent.mkdir(parents=True)
    provider_env.write_text("WARCRAFTLOGS_CLIENT_ID=provider-id\nWARCRAFTLOGS_CLIENT_SECRET=provider-secret\n")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("WARCRAFTLOGS_CLIENT_ID", raising=False)
    monkeypatch.delenv("WARCRAFTLOGS_CLIENT_SECRET", raising=False)

    auth = load_warcraftlogs_auth_config(start_dir=str(tmp_path))

    assert auth.configured is True
    assert auth.client_id == "provider-id"
    assert auth.client_secret == "provider-secret"
    assert auth.env_file == str(provider_env)


def test_warcraftlogs_auth_config_does_not_leak_loaded_credentials_into_process_env(monkeypatch, tmp_path) -> None:
    repo_env = tmp_path / ".env.local"
    repo_env.write_text("WARCRAFTLOGS_CLIENT_ID=repo-id\nWARCRAFTLOGS_CLIENT_SECRET=repo-secret\n")

    monkeypatch.delenv("WARCRAFTLOGS_CLIENT_ID", raising=False)
    monkeypatch.delenv("WARCRAFTLOGS_CLIENT_SECRET", raising=False)

    auth = load_warcraftlogs_auth_config(start_dir=str(tmp_path))

    assert auth.configured is True
    assert auth.client_id == "repo-id"
    assert auth.client_secret == "repo-secret"
    assert "WARCRAFTLOGS_CLIENT_ID" not in os.environ
    assert "WARCRAFTLOGS_CLIENT_SECRET" not in os.environ


def test_warcraftlogs_pkce_exchange_uses_client_auth(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_warcraftlogs_auth_config",
        lambda start_dir=None: type(
            "Auth",
            (),
            {
                "configured": True,
                "client_id": "client-id",
                "client_secret": "client-secret",
                "env_file": "/tmp/.env.local",
            },
        )(),
    )

    captured: dict[str, object] = {}

    def _fake_request(client, url, *, method="GET", data=None, auth=None, retry_attempts=1, **kwargs):  # noqa: ANN001
        captured["url"] = url
        captured["method"] = method
        captured["data"] = data
        captured["auth"] = auth
        return httpx.Response(
            200,
            json={"access_token": "token", "refresh_token": "refresh"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("warcraftlogs_cli.client.request_with_retries", _fake_request)

    client = WarcraftLogsClient()
    try:
        payload = client.exchange_pkce_code(
            code="code-123",
            redirect_uri="http://127.0.0.1:8787/callback",
            code_verifier="verifier-123",
        )
    finally:
        client.close()

    assert payload["access_token"] == "token"
    assert captured["method"] == "POST"


def test_warcraftlogs_client_public_token_requires_client_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_warcraftlogs_auth_config",
        lambda start_dir=None: type(
            "Auth",
            (),
            {
                "configured": False,
                "client_id": None,
                "client_secret": None,
                "env_file": None,
            },
        )(),
    )
    client = WarcraftLogsClient()
    try:
        with pytest.raises(WarcraftLogsClientError) as exc_info:
            client._token()
    finally:
        client.close()

    assert exc_info.value.code == "missing_public_auth"
    assert "WARCRAFTLOGS_CLIENT_ID" in exc_info.value.message


def test_warcraftlogs_client_reuses_shared_public_token_across_instances(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_warcraftlogs_auth_config",
        lambda start_dir=None: type(
            "Auth",
            (),
            {
                "configured": True,
                "client_id": "client-id",
                "client_secret": "client-secret",
                "env_file": "/tmp/.env.local",
            },
        )(),
    )

    saved_state: dict[str, dict[str, object]] = {}

    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_provider_auth_state",
        lambda provider: saved_state.get(provider),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.client.save_provider_auth_state",
        lambda provider, payload, path=None: saved_state.setdefault(provider, dict(payload)) and Path("/tmp/shared-token.json"),
    )

    token_requests: list[str] = []

    def _fake_request(client, url, *, method="GET", data=None, auth=None, retry_attempts=1, **kwargs):  # noqa: ANN001
        del client, data, auth, retry_attempts, kwargs
        token_requests.append(url)
        return httpx.Response(
            200,
            json={"access_token": "public-token", "expires_in": 3600},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("warcraftlogs_cli.client.request_with_retries", _fake_request)

    first = WarcraftLogsClient()
    second = WarcraftLogsClient()
    try:
        assert first._token() == "public-token"
        assert second._token() == "public-token"
    finally:
        first.close()
        second.close()

    assert token_requests == ["https://www.warcraftlogs.com/oauth/token"]
    assert saved_state["warcraftlogs-client-credentials"]["auth_mode"] == "client_credentials"


def test_warcraftlogs_client_ignores_invalid_shared_public_token_state(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_warcraftlogs_auth_config",
        lambda start_dir=None: type(
            "Auth",
            (),
            {
                "configured": True,
                "client_id": "client-id",
                "client_secret": "client-secret",
                "env_file": "/tmp/.env.local",
            },
        )(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_provider_auth_state",
        lambda provider: (_ for _ in ()).throw(json.JSONDecodeError("bad json", "{", 1)),
    )

    token_requests: list[str] = []

    def _fake_request(client, url, *, method="GET", data=None, auth=None, retry_attempts=1, **kwargs):  # noqa: ANN001
        del client, data, auth, retry_attempts, kwargs
        token_requests.append(url)
        return httpx.Response(
            200,
            json={"access_token": "public-token", "expires_in": 3600},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("warcraftlogs_cli.client.request_with_retries", _fake_request)

    client = WarcraftLogsClient()
    try:
        assert client._token() == "public-token"
    finally:
        client.close()

    assert token_requests == ["https://www.warcraftlogs.com/oauth/token"]


def test_warcraftlogs_client_live_user_probe_does_not_write_shared_cache(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_warcraftlogs_auth_config",
        lambda start_dir=None: type(
            "Auth",
            (),
            {
                "configured": False,
                "client_id": None,
                "client_secret": None,
                "env_file": None,
            },
        )(),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_provider_auth_state",
        lambda provider: {
            "auth_mode": "pkce",
            "access_token": "user-token",
            "expires_at": time.time() + 3600,
        },
    )

    def _fake_request(client, url, *, method="GET", data=None, auth=None, retry_attempts=1, **kwargs):  # noqa: ANN001
        del client, data, auth, retry_attempts
        assert method == "POST"
        assert url.endswith("/api/v2/user")
        return httpx.Response(
            200,
            json={"data": {"userData": {"currentUser": {"id": 55, "name": "Auro", "avatar": "https://assets.example/avatar.png"}}}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("warcraftlogs_cli.client.request_with_retries", _fake_request)

    writes: list[tuple[str, object, int]] = []
    client = WarcraftLogsClient()
    monkeypatch.setattr(
        client,
        "_write_cache",
        lambda key, payload, *, ttl_seconds: writes.append((key, payload, ttl_seconds)),
    )
    try:
        payload = client.probe_live_user_api()
    finally:
        client.close()

    assert payload["name"] == "Auro"
    assert writes == []


def test_warcraftlogs_client_live_public_probe_does_not_write_shared_cache(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_warcraftlogs_auth_config",
        lambda start_dir=None: type(
            "Auth",
            (),
            {
                "configured": True,
                "client_id": "client-id",
                "client_secret": "client-secret",
                "env_file": "/tmp/.env.local",
            },
        )(),
    )

    def _fake_request(client, url, *, method="GET", data=None, auth=None, retry_attempts=1, **kwargs):  # noqa: ANN001
        del client, retry_attempts, kwargs
        if url.endswith("/oauth/token"):
            assert method == "POST"
            return httpx.Response(
                200,
                json={"access_token": "public-token", "expires_in": 3600},
                request=httpx.Request("POST", url),
            )
        assert method == "POST"
        assert url.endswith("/api/v2/client")
        assert auth is None
        assert data is None
        return httpx.Response(
            200,
            json={"data": {"rateLimitData": {"limitPerHour": 3600, "pointsSpentThisHour": 42, "pointsResetIn": 1800}}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("warcraftlogs_cli.client.request_with_retries", _fake_request)

    writes: list[tuple[str, object, int]] = []
    client = WarcraftLogsClient()
    monkeypatch.setattr(
        client,
        "_write_cache",
        lambda key, payload, *, ttl_seconds: writes.append((key, payload, ttl_seconds)),
    )
    try:
        payload = client.probe_live_public_api()
    finally:
        client.close()

    assert payload["limitPerHour"] == 3600
    assert writes == []


def test_warcraftlogs_report_fights_requires_public_auth_not_generic_missing_auth(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_warcraftlogs_auth_config",
        lambda start_dir=None: type(
            "Auth",
            (),
            {
                "configured": False,
                "client_id": None,
                "client_secret": None,
                "env_file": None,
            },
        )(),
    )
    monkeypatch.setattr("warcraftlogs_cli.client.load_provider_auth_state", lambda provider: None)

    result = runner.invoke(warcraftlogs_app, ["report-fights", "abcd1234"])
    assert result.exit_code == 1

    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_public_auth"


def test_warcraftlogs_client_has_user_token_returns_false_when_state_missing(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.client.load_provider_auth_state", lambda provider: None)
    client = WarcraftLogsClient.__new__(WarcraftLogsClient)
    assert client._has_user_token() is False


def test_warcraftlogs_client_has_user_token_returns_false_for_client_credentials_state(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_provider_auth_state",
        lambda provider: {"auth_mode": "client_credentials", "access_token": "ignored"},
    )
    client = WarcraftLogsClient.__new__(WarcraftLogsClient)
    assert client._has_user_token() is False


def test_warcraftlogs_client_has_user_token_returns_false_when_expired(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_provider_auth_state",
        lambda provider: {
            "auth_mode": "authorization_code",
            "access_token": "tok",
            "expires_at": time.time() - 1,
        },
    )
    client = WarcraftLogsClient.__new__(WarcraftLogsClient)
    assert client._has_user_token() is False


def test_warcraftlogs_client_has_user_token_returns_true_for_valid_user_token(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_provider_auth_state",
        lambda provider: {
            "auth_mode": "pkce",
            "access_token": "tok",
            "expires_at": time.time() + 3600,
        },
    )
    client = WarcraftLogsClient.__new__(WarcraftLogsClient)
    assert client._has_user_token() is True


def test_warcraftlogs_client_graphql_routes_through_user_endpoint_when_authenticated(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_provider_auth_state",
        lambda provider: {
            "auth_mode": "pkce",
            "access_token": "user-token",
            "expires_at": time.time() + 3600,
        },
    )
    client = WarcraftLogsClient.__new__(WarcraftLogsClient)
    captured: dict[str, object] = {}

    def _fake_user(*, operation_name, query, variables, namespace, ttl_seconds, use_cache=True):
        captured["operation_name"] = operation_name
        captured["namespace"] = namespace
        captured["ttl_seconds"] = ttl_seconds
        captured["use_cache"] = use_cache
        return {"ok": True}

    def _fail_client(**_kwargs):
        raise AssertionError("client endpoint must not be hit when a user token is saved")

    client._graphql_user = _fake_user
    monkeypatch.setattr(client, "_token", _fail_client)

    payload = client._graphql(
        operation_name="OpName",
        query="query Q { x }",
        variables={"a": 1},
        namespace="ns",
        ttl_seconds=42,
    )

    assert payload == {"ok": True}
    assert captured["operation_name"] == "OpName"
    assert captured["namespace"] == "ns"
    assert captured["ttl_seconds"] == 42
    assert captured["use_cache"] is True


def test_warcraftlogs_client_graphql_uses_client_endpoint_without_user_token(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.client.load_provider_auth_state", lambda provider: None)
    client = WarcraftLogsClient.__new__(WarcraftLogsClient)
    client._site = RETAIL_PROFILE
    client._cache_store = None
    client._retry_attempts = 1
    client._http_client = None
    client._timeout_seconds = 5.0
    client._access_token = "client-token"
    client._token_expires_at = time.time() + 3600

    def _fail_user(**_kwargs):
        raise AssertionError("user endpoint must not be hit without a saved user token")

    client._graphql_user = _fail_user

    captured: dict[str, object] = {}

    class _Resp:
        def json(self) -> dict[str, object]:
            return {"data": {"ok": True}}

    def _fake_request(http_client, url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return _Resp()

    monkeypatch.setattr("warcraftlogs_cli.client.request_with_retries", _fake_request)

    payload = client._graphql(
        operation_name="OpName",
        query="query Q { x }",
        variables=None,
        namespace="ns",
        ttl_seconds=42,
    )

    assert payload == {"ok": True}
    assert captured["url"] == RETAIL_PROFILE.api_url
    assert captured["headers"]["Authorization"] == "Bearer client-token"


def test_warcraftlogs_client_graphql_user_and_client_caches_do_not_collide() -> None:
    client = WarcraftLogsClient.__new__(WarcraftLogsClient)
    client._site = RETAIL_PROFILE

    common = {
        "operation_name": "OpName",
        "query": "query Q { x }",
        "variables": {"a": 1},
    }
    user_key = client._cache_key("ns", {"endpoint": "user", **common})
    client_key = client._cache_key("ns", {"endpoint": "client", **common})
    assert user_key != client_key


def test_warcraftlogs_auth_status_flags_missing_view_user_profile_scope(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state-home" / "warcraft" / "providers"
    state_dir.mkdir(parents=True)
    state_file = state_dir / "warcraftlogs.json"
    state_file.write_text(
        json.dumps(
            {
                "auth_mode": "authorization_code",
                "access_token": "tok",
                "expires_at": time.time() + 3600,
                "scope": "",
                "requested_scopes": [],
            }
        )
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _FakeWarcraftLogsClient)
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": True, "env_file": "/tmp/.env.local"})(),
    )

    result = runner.invoke(warcraftlogs_app, ["auth", "status"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    user_api = payload["auth"]["user_api_access"]
    assert user_api["scopes"]["has_view_user_profile"] is False
    assert "view-user-profile" in user_api["scope_warning"]


def test_warcraftlogs_auth_status_no_scope_warning_when_view_user_profile_present(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state-home" / "warcraft" / "providers"
    state_dir.mkdir(parents=True)
    state_file = state_dir / "warcraftlogs.json"
    state_file.write_text(
        json.dumps(
            {
                "auth_mode": "authorization_code",
                "access_token": "tok",
                "expires_at": time.time() + 3600,
                "scope": "view-user-profile",
                "requested_scopes": ["view-user-profile"],
            }
        )
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setattr("warcraftlogs_cli.main.WarcraftLogsClient", _FakeWarcraftLogsClient)
    monkeypatch.setattr(
        "warcraftlogs_cli.main.load_warcraftlogs_auth_config",
        lambda: type("Auth", (), {"configured": True, "env_file": "/tmp/.env.local"})(),
    )

    result = runner.invoke(warcraftlogs_app, ["auth", "status"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    user_api = payload["auth"]["user_api_access"]
    assert user_api["scopes"]["has_view_user_profile"] is True
    assert user_api["scope_warning"] is None


def test_warcraftlogs_auth_login_token_exchange_surfaces_scope_warning_when_requested_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    state_file = tmp_path / "state-home" / "warcraft" / "providers" / "warcraftlogs.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps(
            {
                "pending_auth_mode": "authorization_code",
                "pending_state": "pending-state-123",
                "redirect_uri": "http://127.0.0.1:8787/callback",
                "requested_scopes": [],
            }
        )
    )
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _FakeWarcraftLogsClient())
    monkeypatch.setattr("warcraftlogs_cli.main.time.time", lambda: 1000.0)

    result = runner.invoke(
        warcraftlogs_app,
        [
            "auth",
            "login",
            "--redirect-uri",
            "http://127.0.0.1:8787/callback",
            "--code",
            "code-123",
            "--state",
            "pending-state-123",
        ],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["step"] == "token_exchanged"
    assert payload["scopes"]["granted"] == ["reports"]
    assert payload["scopes"]["requested"] == []
    assert payload["scopes"]["has_view_user_profile"] is False
    assert "view-user-profile" in payload["scope_warning"]


def test_warcraftlogs_auth_pkce_login_token_exchange_clears_warning_when_view_user_profile_granted(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    state_file = tmp_path / "state-home" / "warcraft" / "providers" / "warcraftlogs.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps(
            {
                "pending_auth_mode": "pkce",
                "pending_state": "pending-state-456",
                "redirect_uri": "http://127.0.0.1:8787/callback",
                "code_verifier": "verifier-123",
                "requested_scopes": ["view-user-profile"],
            }
        )
    )

    class _ScopedFakeClient(_FakeWarcraftLogsClient):
        def exchange_pkce_code(self, *, code: str, redirect_uri: str, code_verifier: str) -> dict[str, object]:
            assert code == "code-456"
            return {
                "access_token": "pkce-token",
                "refresh_token": "pkce-refresh",
                "token_type": "Bearer",
                "scope": "view-user-profile",
                "expires_in": 3600,
            }

    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _ScopedFakeClient())
    monkeypatch.setattr("warcraftlogs_cli.main.time.time", lambda: 1000.0)

    result = runner.invoke(
        warcraftlogs_app,
        [
            "auth",
            "pkce-login",
            "--redirect-uri",
            "http://127.0.0.1:8787/callback",
            "--code",
            "code-456",
            "--state",
            "pending-state-456",
        ],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["step"] == "token_exchanged"
    assert payload["scopes"]["granted"] == ["view-user-profile"]
    assert payload["scopes"]["requested"] == ["view-user-profile"]
    assert payload["scopes"]["has_view_user_profile"] is True
    assert payload["scope_warning"] is None


def test_warcraftlogs_auth_token_includes_scope_breakdown(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state-home" / "warcraft" / "providers"
    state_dir.mkdir(parents=True)
    state_file = state_dir / "warcraftlogs.json"
    state_file.write_text(
        json.dumps(
            {
                "auth_mode": "pkce",
                "access_token": "tok",
                "expires_at": time.time() + 3600,
                "scope": "view-user-profile",
                "requested_scopes": ["view-user-profile"],
            }
        )
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))

    result = runner.invoke(warcraftlogs_app, ["auth", "token"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["token"]["endpoint_family"] == "user"
    assert payload["token"]["scopes"]["granted"] == ["view-user-profile"]
    assert payload["token"]["scopes"]["has_view_user_profile"] is True
    assert payload["token"]["scope_warning"] is None


def test_warcraftlogs_auth_token_warns_when_view_user_profile_missing(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state-home" / "warcraft" / "providers"
    state_dir.mkdir(parents=True)
    state_file = state_dir / "warcraftlogs.json"
    state_file.write_text(
        json.dumps(
            {
                "auth_mode": "authorization_code",
                "access_token": "tok",
                "expires_at": time.time() + 3600,
                "scope": None,
                "requested_scopes": [],
            }
        )
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))

    result = runner.invoke(warcraftlogs_app, ["auth", "token"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["token"]["scopes"]["granted"] == []
    assert payload["token"]["scopes"]["has_view_user_profile"] is False
    assert "view-user-profile" in payload["token"]["scope_warning"]


def test_warcraftlogs_auth_token_survives_corrupt_state_file(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state-home" / "warcraft" / "providers"
    state_dir.mkdir(parents=True)
    (state_dir / "warcraftlogs.json").write_text("{not-valid-json")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))

    result = runner.invoke(warcraftlogs_app, ["auth", "token"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["token"]["endpoint_family"] == "client"
    assert payload["token"]["scopes"]["granted"] == []
    assert payload["token"]["scopes"]["has_view_user_profile"] is False


def test_warcraftlogs_client_has_user_token_survives_corrupt_state_file(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state-home" / "warcraft" / "providers"
    state_dir.mkdir(parents=True)
    (state_dir / "warcraftlogs.json").write_text("{not-valid-json")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))

    client = WarcraftLogsClient.__new__(WarcraftLogsClient)
    assert client._has_user_token() is False


def test_warcraftlogs_client_rate_limit_always_uses_client_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_provider_auth_state",
        lambda provider: {
            "auth_mode": "pkce",
            "access_token": "user-token",
            "expires_at": time.time() + 3600,
        },
    )
    client = WarcraftLogsClient.__new__(WarcraftLogsClient)
    client._site = RETAIL_PROFILE
    client._cache_store = None
    client._retry_attempts = 1
    client._http_client = None
    client._timeout_seconds = 5.0
    client._access_token = "client-token"
    client._token_expires_at = time.time() + 3600

    def _fail_user(**_kwargs):
        raise AssertionError("rate_limit must not route through the user endpoint")

    client._graphql_user = _fail_user

    captured: dict[str, object] = {}

    class _Resp:
        def json(self) -> dict[str, object]:
            return {"data": {"rateLimitData": {"limitPerHour": 100, "pointsSpentThisHour": 1, "pointsResetIn": 60}}}

    def _fake_request(http_client, url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return _Resp()

    monkeypatch.setattr("warcraftlogs_cli.client.request_with_retries", _fake_request)

    payload = client.rate_limit()

    assert payload["limitPerHour"] == 100
    assert captured["url"] == RETAIL_PROFILE.api_url
    assert captured["headers"]["Authorization"] == "Bearer client-token"


def test_warcraftlogs_client_probe_live_public_api_always_uses_client_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraftlogs_cli.client.load_provider_auth_state",
        lambda provider: {
            "auth_mode": "pkce",
            "access_token": "user-token",
            "expires_at": time.time() + 3600,
        },
    )
    client = WarcraftLogsClient.__new__(WarcraftLogsClient)
    client._site = RETAIL_PROFILE
    client._cache_store = None
    client._retry_attempts = 1
    client._http_client = None
    client._timeout_seconds = 5.0
    client._access_token = "client-token"
    client._token_expires_at = time.time() + 3600

    def _fail_user(**_kwargs):
        raise AssertionError("probe_live_public_api must not route through the user endpoint")

    client._graphql_user = _fail_user

    captured: dict[str, object] = {}

    class _Resp:
        def json(self) -> dict[str, object]:
            return {"data": {"rateLimitData": {"limitPerHour": 100, "pointsSpentThisHour": 1, "pointsResetIn": 60}}}

    def _fake_request(http_client, url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return _Resp()

    monkeypatch.setattr("warcraftlogs_cli.client.request_with_retries", _fake_request)

    payload = client.probe_live_public_api()

    assert payload["limitPerHour"] == 100
    assert captured["url"] == RETAIL_PROFILE.api_url
    assert captured["headers"]["Authorization"] == "Bearer client-token"

# Contract test catalog

Pinned inputs for recorded and live contract tests. Update fixtures when pages age out or parsers drift — see [FIXTURE_MAINTENANCE.md](FIXTURE_MAINTENANCE.md).

## Wowhead parser canaries

Source: `tests/fixtures/wowhead_canaries.py`  
Runner: `tests/test_wowhead_parser_canaries.py` (live, `WOWHEAD_LIVE_TESTS=1`)

| Case ID | Expansion | Entity | ID | Why pinned |
| --- | --- | --- | --- | --- |
| `retail-item` | retail | item | 19019 | Legendary sword; stable tooltip + page parser coverage |
| `retail-npc` | retail | npc | 12056 | Classic raid NPC; href-linked entity extraction |
| `retail-spell` | retail | spell | 40827 | Common spell page shape |
| `retail-quest` | retail | quest | 76487 | Quest page with objectives block |
| `retail-object` | retail | object | 181332 | Object/chest entity type |
| `wotlk-item` | wotlk | item | 49623 | Expansion-prefixed item URL |
| `classic-item` | classic | item | 19019 | Classic Era prefix routing |

## Wowhead recorded expansion fixtures

Source: `tests/fixtures/expansion_recorded.json`  
Runner: `tests/test_expansion_recorded_fixtures.py`, `tests/test_wowhead_schema_snapshots.py`

| Profile | Entity | ID | Notes |
| --- | --- | --- | --- |
| all keys in fixture | item | 19019 | Thunderfury; search, tooltip, entity-page, comments recorded per expansion profile |

## Warcraft Logs live matrix

Source: `tests/fixtures/live_matrix.py`, `tests/fixtures/wcl_matrix_cases.py`  
Runner: `tests/test_live_command_matrix.py` (`make test-live-matrix`)

| Input | Value | Why pinned |
| --- | --- | --- |
| `PUBLIC_REPORT_CODE` | `qQVdxDcWyB3wGznL` | Heroic/Mythic retail report with stable fights for matrix commands |
| `PRIVATE_REPORT_CODE` | `7Rc3HPCWGYy1z4tT` | User-auth private report slice (skipped without token) |
| `ZONE_ID` / `ENCOUNTER_PLEXUS` | 44 / 3129 | Manaforge Omega sampled analytics scope |
| `GUILD_*` | us / malganis / gn | Guild search and profile commands |

## Schema snapshot keys

Source: `tests/fixtures/wowhead_output_schemas.py`  
Runner: `tests/test_wowhead_schema_snapshots.py`

Documents required top-level JSON keys per command (`search`, `entity`, `entity-page`, `comments`, `compare`). Item entity commands also require `schema_version` and `normalized` via `ENTITY_ITEM_KEYS`.

## Related

- [FIXTURE_MAINTENANCE.md](FIXTURE_MAINTENANCE.md)
- [../wowhead/CONTRACTS.md](../wowhead/CONTRACTS.md)
- [LINTING_AND_COMPLEXITY.md](LINTING_AND_COMPLEXITY.md)

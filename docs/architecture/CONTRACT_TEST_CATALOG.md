# Contract test catalog

Pinned inputs for offline and live contract tests, plus the cross-provider tests that hold the
shared contracts. Update fixtures when pages age out or parsers drift — see
[FIXTURE_MAINTENANCE.md](FIXTURE_MAINTENANCE.md) for the synthetic vs captured distinction.

## Wowhead parser canaries

Source: `tests/fixtures/wowhead_canaries.py`  
Runner: `tests/test_wowhead_parser_canaries.py` (live, `WOWHEAD_LIVE_TESTS=1`)

| Case ID | Expansion | Entity | ID | Why pinned |
| --- | --- | --- | --- | --- |
| `retail-item` | retail | item | 19019 | Legendary sword; stable tooltip + page parser coverage |
| `retail-npc` | retail | npc | 12056 | Classic raid NPC; href-linked entity extraction |
| `retail-spell` | retail | spell | 40827 | Common spell page shape |
| `retail-quest` | retail | quest | 5441 | Quest page with objectives block; vanilla Durotar quest that survived the Cataclysm revamp, so non-seasonal and long-lived (76487 was removed from the client DB and now 404s) |
| `retail-object` | retail | object | 181332 | Object/chest entity type |
| `wotlk-item` | wotlk | item | 49623 | Expansion-prefixed item URL |
| `classic-item` | classic | item | 19019 | Classic Era prefix routing |

## Wowhead synthetic expansion fixtures

Source: `tests/fixtures/expansion_synthetic.json` (hand-written, not captured)  
Runner: `tests/test_expansion_synthetic_fixtures.py`, `tests/test_wowhead_schema_snapshots.py`

| Profile | Entity | ID | Notes |
| --- | --- | --- | --- |
| all keys in fixture | item | 19019 | Thunderfury; pins search, tooltip, entity-page, and comment routing per expansion profile |

## Provider page fixtures

| Source | Kind | Runner |
| --- | --- | --- |
| `tests/fixtures/method/*.html` | synthetic (hand-written) | `tests/test_method_synthetic_fixtures.py` |
| `tests/fixtures/icy_veins/*.html` | captured real pages (trimmed) | `tests/test_icy_veins_recorded_fixtures.py`, `tests/test_icy_veins_cli.py` |
| `tests/fixtures/blizzard/*.json` | synthetic (hand-written) | `tests/test_blizzard_api_contracts.py` |
| `tests/fixtures/curseforge/*.json` | synthetic (hand-written) | `tests/test_curseforge_contracts.py` |

## Warcraft Logs live matrix

Source: `tests/fixtures/live_matrix.py`, `tests/fixtures/wcl_matrix_cases.py`  
Runner: `tests/test_live_command_matrix.py` (`make test-live-matrix`)

Retail tiers roll over and reports age out of retention, so the matrix pins identities only and
discovers every volatile input at runtime. Nothing in this section goes stale when a tier ends.

| Pinned input | Value | Why pinned |
| --- | --- | --- |
| `GUILD_REGION` / `GUILD_REALM` / `GUILD_NAME` | us / malganis / gn | Guild profile, roster, attendance, and private-report commands |
| `CHARACTER_NAME` | Aurow | Character profile and character-rankings commands |
| `ANCHOR_DIFFICULTIES` | (4, 5) | Heroic then Mythic: the ranked difficulties an anchor kill may use |
| `RAID_DIFFICULTY_IDS` | {3, 4, 5} | Marks a zone as a raid (Mythic+ and Delves zones expose other IDs) |
| `DISCOVERY_REPORT_LIMIT` / `SAMPLE_*` | 10 / 1 page x 25 / ±1000 ms | Discovery scan depth and sampled-analytics cohort size |

| Discovered at runtime | How |
| --- | --- |
| Zone | Newest non-frozen zone whose difficulties include the raid triple (`zones`) |
| Boss + difficulty | First Heroic-then-Mythic kill in a recent public report of that zone (`reports` + `report-fights`) |
| Public report + fight | The report that kill came from — the anchor for every `report-*` case |
| Sampled cohort | `--start-time` / `--end-time` window centred on the anchor report's start, so the cohort provably contains the anchor kill |
| Aura / actor / cast ability IDs | `report-encounter-buffs`, `report-player-details`, `report-events` on the anchor kill |
| Private report | The guild's most recent `visibility: private` report (skipped without user auth) |

Each case declares a JSON path plus a `DataCheck` (`NONEMPTY`, `POSITIVE`, `PRESENT`, `TRUE`,
`SAMPLING_METADATA`). The path must exist — a missing leaf fails instead of silently falling back to
the canonical block, and a scalar never passes a collection check. `SAMPLING_METADATA` is reserved
for cohorts that may legitimately be empty (a single-spec filter); those cases still assert that
`sample_scope.filters` echoes the requested zone/boss/difficulty, that `sample_scope.returned` is a
count, and that `citations`, `freshness.sampled_at`, and `cache_provenance.finished` are served.

`tests/test_warcraftlogs_live.py` keeps `FROZEN_ZONE_ID` / `FROZEN_BOSS_ID` (44 / 3129, Manaforge
Omega) locally: those tests assert trust-block and cohort *shape*, so they want a tier whose reports
stay put rather than the churning current one.

## Blizzard API contracts

Source: `tests/fixtures/blizzard/{realm,item,character}.json`  
Runner: `tests/test_blizzard_api_contracts.py` (offline; monkeypatches `request_with_retries`)  
Live runner: `tests/test_blizzard_api_live.py` (`BLIZZARD_LIVE_TESTS=1`, skipped without credentials)

The offline contract tests serve token payloads for `/token` POSTs and fixtures for API GETs, and
cover the success envelope + provenance, per-region/namespace routing (`dynamic`/`static`/`profile`,
retail + `--classic`), the `missing_client_credentials` / `unsupported_region` /
`unsupported_game_version` / `classic_profile_unsupported` / `http_error` / `network_error` /
`invalid_response` error envelopes, and single-token reuse across both in-memory and shared-state
caches. The live runner is the one-time spike that confirms the documented hosts + namespace strings.

## Schema snapshot keys

Source: `tests/fixtures/wowhead_output_schemas.py`  
Runner: `tests/test_wowhead_schema_snapshots.py`

Documents required top-level JSON keys per command (`search`, `entity`, `entity-page`, `comments`, `compare`). Item entity commands also require `schema_version` and `normalized` via `ENTITY_ITEM_KEYS`.

## Cross-provider contract tests

These hold the contracts that span every binary. They take no pinned provider input, so they never
go stale — they fail when the code or the docs drift.

| Test | What it holds |
| --- | --- |
| `tests/test_envelope_conformance.py` | Every provider's `doctor`/`search`/`resolve` returns a conforming envelope; every provider is in exactly one tier |
| `tests/test_cli_error_contract.py` | A transport failure on every binary produces a JSON error envelope with the mapped exit code, never a traceback |
| `tests/test_cli_help_parity.py` | Every command, positional argument, and root app on every binary has help text |
| `tests/test_docs_parity.py` | Every shell example in `README.md`, `docs/`, and `skills/` names a command and flags the CLIs actually have, with global flags before the subcommand |
| `tests/test_command_reference.py` | `docs/reference/<cli>.md` matches what `make reference` generates from the Typer apps |
| `tests/test_generate_provider_skills.py` | The skill generator covers every provider in the wrapper registry |
| `tests/test_warcraft_cli_packaging.py` | Each package's declared dependencies match its imports; one console script per provider; the root wheel exposes all 13 |
| `tests/test_repo_tooling.py` | Makefile live-test env flags match the conftest registry |
| `tests/test_network_guard.py` | The conftest guard actually blocks network access for non-live tests |
| `tests/test_provider_contract.py` | Wrapper search ranking, query intent detection, and candidate shaping |

## Related

- [FIXTURE_MAINTENANCE.md](FIXTURE_MAINTENANCE.md)
- [../foundation/ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md)
- [../wowhead/CONTRACTS.md](../wowhead/CONTRACTS.md)
- [LINTING_AND_COMPLEXITY.md](LINTING_AND_COMPLEXITY.md)

## End-to-end journeys

`tests/e2e/` runs every binary as a subprocess against real providers; see
[E2E_TESTING.md](E2E_TESTING.md). Its only pins are the permanent identifiers in `tests/e2e/pins.py`;
everything else is discovered at run time.

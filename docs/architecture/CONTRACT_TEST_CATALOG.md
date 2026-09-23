# Contract test catalog

Pinned inputs for offline and live contract tests, plus the cross-provider tests that hold the
shared contracts. Update fixtures when pages age out or parsers drift — see
[FIXTURE_MAINTENANCE.md](FIXTURE_MAINTENANCE.md) for the synthetic vs captured distinction.

## Wowhead parser canaries

Source: `tests/fixtures/wowhead_canaries.py`  
Runner: `tests/test_wowhead_parser_canaries.py` (live, `make test-canary`)

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

## Blizzard API contracts

Source: `tests/fixtures/blizzard/{realm,item,character}.json`  
Runner: `tests/test_blizzard_api_contracts.py` (offline; monkeypatches `request_with_retries`)  
Live runner: `tests/e2e/test_blizzard.py` (`make test-e2e`, needs credentials)

The offline contract tests serve token payloads for `/token` POSTs and fixtures for API GETs, and
cover the success envelope + provenance, per-region/namespace routing (`dynamic`/`static`/`profile`,
retail + `--classic`), the `missing_client_credentials` / `unsupported_region` /
`unsupported_game_version` / `classic_profile_unsupported` / `http_error` / `network_error` /
`invalid_response` error envelopes, and single-token reuse across both in-memory and shared-state
caches. The live runner confirms the documented hosts + namespace strings.

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

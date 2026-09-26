# Fixture maintenance

The pinned data behind the offline tests, how to refresh it, and the cross-provider contract tests
that hold the shared contracts.

Fixtures come in two kinds and they are maintained differently:

- **Synthetic fixtures** are hand-written. They pin routing, URL construction, and payload key
  presence. There is no capture step and no recorder script; you edit them by hand.
- **Captured fixtures** are real provider responses saved to disk. They pin parsers and ranking
  against what the provider actually serves. They are re-captured from the live source and then
  trimmed, never hand-edited, except where a section below says so.

Non-live tests run under the socket-level network guard in `tests/conftest.py`, so a fixture test
that accidentally reaches the network fails instead of silently going live. Capture work therefore
happens outside pytest.

## When to refresh

- A parser canary fails with HTTP 404
- `wowhead doctor` live probes fail for a pinned expansion profile
- A schema snapshot test fails after an intentional CLI output change (update the fixture or the
  expected keys, and record the change in `CHANGELOG.md`)
- A provider redesigns the page or response a captured fixture came from

## Inventory

| Source | Kind | Tests |
| --- | --- | --- |
| `tests/fixtures/expansion_synthetic.json` | synthetic | `test_expansion_synthetic_fixtures.py`, `test_wowhead_schema_snapshots.py` |
| `tests/fixtures/wowhead_output_schemas.py` | synthetic (required `data` keys per Wowhead command) | `test_wowhead_schema_snapshots.py` |
| `tests/fixtures/wowhead_canaries.py` | pinned live entities | `test_wowhead_parser_canaries.py` (live, `make test-canary`) |
| `tests/fixtures/wowhead/` | captured | `test_wowhead_captured_fixtures.py`, `test_wowhead_tools.py` |
| `tests/fixtures/method/*.html` | synthetic, plus one captured page | `test_method_synthetic_fixtures.py`, `test_method_captured_fixtures.py` |
| `tests/fixtures/icy_veins/*.html` | captured | `test_icy_veins_recorded_fixtures.py`, `test_icy_veins_cli.py` |
| `tests/fixtures/raiderio/` | captured | `test_raiderio_captured_fixtures.py` |
| `tests/fixtures/warcraft_wiki/` | captured | `test_warcraft_wiki_parser.py`, `test_warcraft_wiki_cli.py` |
| `tests/fixtures/lorrgs/` | captured | `test_lorrgs_cli.py` |
| `tests/fixtures/warcraftlogs/` | captured | `test_warcraftlogs_captured_fixtures.py` |
| `tests/fixtures/simc/` | captured, plus one synthetic decode log (`dh_decode_debug.txt`) | `test_simc_cli.py`, `test_simc_build_input.py`, `test_simc_compare.py` |
| `tests/fixtures/blizzard/*.json` | synthetic | `test_blizzard_api_contracts.py` |
| `tests/fixtures/curseforge/*.json` | synthetic | `test_curseforge_contracts.py` |

## Synthetic: Wowhead routing fixtures

`tests/fixtures/expansion_synthetic.json` holds only what expansion routing needs: `query`,
`profiles` (each with `data_env`, `canonical_url`, `link_href`), `search_result`, `tooltip`,
`comment`, `reply_thread`. Entity-page HTML is synthesized in `tests/article_provider_testkit.py`,
not stored.

To change it:

1. Edit the JSON directly, preserving the six top-level keys and the per-profile keys above.
2. Keep one stable item (default: item 19019) across profiles so URLs stay comparable.
3. Verify:

```bash
pytest -q tests/test_expansion_synthetic_fixtures.py tests/test_wowhead_schema_snapshots.py
```

`make fixture-refresh-hints` prints the live URLs for the pinned profiles. It is a lookup aid for
checking that the hand-written URLs still resolve; it does not capture anything.

## Synthetic: Method, Blizzard, and CurseForge

`tests/fixtures/method/*.html` (except `captured_talents_page.html`) are small hand-written pages,
one per content family (`class_guide`, `article_guide`, `delve_guide`, `profession_guide`,
`reputation_guide`, `unsupported_index`). They pin family classification and navigation extraction,
so keep them minimal: add only the markup the assertion needs.

`tests/fixtures/blizzard/{realm,item,character}.json` and
`tests/fixtures/curseforge/{mod,search,changelog}.json` are hand-written API bodies. The offline
contract tests serve them through a monkeypatched transport and cover the success envelope,
provenance, per-region and namespace routing, and the error envelopes. The live shapes are
confirmed by `tests/e2e/test_blizzard.py` and `tests/e2e/test_curseforge.py`.

```bash
pytest -q tests/test_method_synthetic_fixtures.py tests/test_blizzard_api_contracts.py tests/test_curseforge_contracts.py
```

## Captured: Icy Veins and Method pages

`tests/fixtures/icy_veins/*.html` and `tests/fixtures/method/captured_talents_page.html` are real
pages. Do not replace them with hand-written stubs: their point is that the parser survives
production markup.

When re-capturing, save the raw page and then trim it:

- Delete every `<style>...</style>` element.
- Delete every `<script>...</script>` element **except**:
  - `type="application/ld+json"`: the parsers read Article JSON-LD for title, author, and dates
  - (Icy Veins) any script whose body contains both `dataLayer` and `page_type`: the parser reads
    the GTM dataLayer for `page.page_type`
- Keep everything else byte-identical. Do not reserialize through BeautifulSoup.

Aim well under 100 KB per file.

```bash
pytest -q tests/test_icy_veins_cli.py tests/test_icy_veins_recorded_fixtures.py tests/test_method_captured_fixtures.py
```

## Captured: Wowhead responses

`tests/fixtures/wowhead/` holds five HTML pages (item 19019, guide 283, Fury Warrior guide 3087, the
news listing, the blue-tracker listing), the "list doesn't exist or has been removed" page `wowhead profiler` fails
on, and nine search-suggestion JSON responses (`search_suggestions_<query>.json`; each file's
`search` field is the query it was captured for, for example `spirit beast`, singular).

- The suggestion JSON is stored exactly as served. The ranking tests read its `categories` lists as
  ranked rows, so a re-capture must keep `categories` intact.
- The HTML is trimmed: delete every `<style>`, every `<script>` except `type="application/json"`,
  and the notifications push-key, newsletter-signup, and news opt-out JSON blocks no parser reads.
- Third-party comment text is the one edit: commenter display handles become `commenter-<n>`, and
  comment and reply bodies are rewritten word for word with neutral filler, keeping line breaks,
  punctuation, word count, and Wowhead markup tags (`[url=...]`) intact.
- `guide_3087_page.html` ships without comments: its comment and commenter (`g_users`,
  `lv_comments0`) script is removed instead of anonymised. It pins inline `[spell=N]` tokens and
  `[build]` blocks.
- Everything else stays byte-identical.

```bash
pytest -q tests/test_wowhead_captured_fixtures.py tests/test_wowhead_tools.py
```

## Captured: Raider.IO API responses

`tests/fixtures/raiderio/` holds three raw Raider.IO API responses: a guild profile, the raid
rankings for that guild's realm, and one Mythic+ leaderboard page. Trim only the lists: the guild
roster is cut to twelve members and the leaderboard page to its first two runs. Everything else is
re-serialized unedited, so every value a test asserts is one Raider.IO sent.

```bash
pytest -q tests/test_raiderio_captured_fixtures.py
```

## Captured: Warcraft Wiki API responses

`tests/fixtures/warcraft_wiki/*.json` are raw `action=query&list=search` (`search_*.json`) and
`action=parse` (`parse_*.json`) responses from `https://warcraft.wiki.gg/api.php`. Re-capture with
the parameters the client sends (`client.py` `search_articles` / `fetch_article_page`), store the
JSON as served, and keep each file well under 100 KB.

```bash
pytest -q tests/test_warcraft_wiki_parser.py tests/test_warcraft_wiki_cli.py
```

## Captured: Lorrgs roster

`tests/fixtures/lorrgs/{specs,bosses}.json` are the raw `data` payloads of `lorrgs specs` and
`lorrgs bosses`. Re-capture with those two commands when the roster changes. They exist so search
and resolve ranking runs against Lorrgs' real name collisions (two Frost specs, two Salhadaar
encounters, and The Eye of the Jailer vs The Jailer, Zovaal). The bosses route is not stably
ordered, so compare it by slug set, not by row order.

```bash
pytest -q tests/test_lorrgs_cli.py
```

## Captured: Warcraft Logs GraphQL responses

`tests/fixtures/warcraftlogs/*_capture.json` are real `api/v2` responses. Each
file's `_capture` block names the source query, the report code and fight, the capture date, and
how it was trimmed (an event window and row limit, and master data reduced to the actors and
abilities those rows reference). Keep the `_capture` block accurate when re-capturing; the rest of
the file is the response as served, except that a private report's player identities (name, GUID,
server) are replaced, as `report_encounter_aura_buffs_capture.json`'s `_capture.trimmed` records.

```bash
pytest -q tests/test_warcraftlogs_captured_fixtures.py
```

## Captured: SimulationCraft output

`tests/fixtures/simc/` holds real SimulationCraft output: `debug=1` decode logs
(`captured_*_debug.txt`), a `json2` report
(`captured_arcane_mage_json2_report.json`), and the rows of the checkout's generated
`trait_data.inc` and `sc_specialization_data.inc` those captures need. Copy trait and
specialization rows verbatim from `engine/dbc/generated/` in the checkout the captures came from,
and add only the rows a test reads.

`dh_decode_debug.txt` is the exception: a synthetic, hand-written decode log with invented node and
entry ids (`101`-`105`, `201`-`205`) that pins only the line format the decode parser reads.

```bash
pytest -q tests/test_simc_cli.py tests/test_simc_build_input.py tests/test_simc_compare.py
```

## Wowhead parser canaries

`tests/fixtures/wowhead_canaries.py` pins one long-lived entity per case: retail item 19019
(Thunderfury), npc 12056, spell 40827, quest 5441, object 181332, wotlk item 49623, and classic item
19019. When one ages out:

1. Pick a replacement entity on the same expansion and entity type.
2. Update `tests/fixtures/wowhead_canaries.py` (`entity_id`, `label`).
3. Run:

```bash
make test-canary
```

## Cross-provider contract tests

These hold the contracts that span every binary. They take no pinned provider input, so they never
go stale; they fail when the code or the docs drift.

| Test | What it holds |
| --- | --- |
| `tests/test_envelope_conformance.py` | Every provider's `doctor`/`search`/`resolve` returns a conforming envelope; every provider is in exactly one tier |
| `tests/test_cli_error_contract.py` | A transport failure on every binary produces a JSON error envelope with the mapped exit code, never a traceback |
| `tests/test_cli_help_parity.py` | Every command, positional argument, and root app on every binary has help text |
| `tests/test_docs_parity.py` | Every shell example in `README.md`, `docs/`, and `skills/` names a command and flags the CLIs actually have (a piped or redirected example up to its first operator; lines with `$(`, `<(` or backticks are skipped) |
| `tests/test_command_reference.py` | `docs/reference/<cli>.md` matches what `make reference` generates from the Typer apps |
| `tests/test_warcraft_cli_envelope_schema.py` | `schemas/envelope.schema.json` matches what `make schema` generates |
| `tests/test_generate_provider_skills.py` | The skill generator covers every provider in the wrapper registry |
| `tests/test_warcraft_cli_packaging.py` | Each package's declared dependencies match its imports; one console script per provider; the root wheel exposes every console script |
| `tests/test_network_guard.py` | The conftest guard actually blocks network access for non-live tests |
| `tests/test_provider_contract.py` | Wrapper search ranking, query intent detection, and candidate shaping |

`tests/e2e/` runs every binary as a subprocess against real providers; see
[E2E_TESTING.md](E2E_TESTING.md). Its only pins are the permanent identifiers in `tests/e2e/pins.py`.

## After any fixture change

- Run `make test-fast`.
- Note the rotation in the PR or commit message; report codes and entity ids go stale over time.

## Related

- [../foundation/ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md)
- [../wowhead/CONTRACTS.md](../wowhead/CONTRACTS.md)
- [LINTING_AND_COMPLEXITY.md](LINTING_AND_COMPLEXITY.md)

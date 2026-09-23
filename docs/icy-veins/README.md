# Icy Veins CLI

`icy-veins` is a WoW guide/article provider CLI. It discovers guides from the Icy Veins WoW
sitemap, fetches and parses guide pages, and exports multi-page guide bundles that can be queried
offline.

Tier: **supported**. No auth, no API key.

## Commands

| Command | What it does |
| --- | --- |
| `icy-veins doctor` | Reports capabilities and the resolved HTTP cache configuration. |
| `icy-veins search <query>` | Ranks sitemap guides against a free-text query. |
| `icy-veins resolve <query>` | Picks the single best guide and returns a `next_command`. |
| `icy-veins guide <guide_ref>` | Fetches one guide page and returns a summary with previews. |
| `icy-veins guide-full <guide_ref>` | Fetches every page in the guide's family navigation and merges them. |
| `icy-veins guide-export <guide_ref>` | Writes the full bundle (pages, entities, analysis surfaces) to a directory. |
| `icy-veins guide-query <bundle> <query>` | Searches an exported bundle without touching the network. |

`guide_ref` accepts a slug (`mistweaver-monk-pve-healing-guide`) or a full
`https://www.icy-veins.com/wow/<slug>` URL.

## Flags

Global flags come before the subcommand and are the shared agent-output flags:
`--pretty`, `--compact`, `--compact-max-chars`, `--fields`, `--fields-strict`, `--profile`.

Per-command flags:

- `search` / `resolve`: `--limit` (1-50, default 5)
- `guide-export`: `--out <dir>` (defaults to `./icy-veins_exports/guide-<slug>`)
- `guide-query`: `--limit` (1-50, default 5), `--kind` (repeatable or comma-separated), `--section-title`

`--kind` accepts `sections`, `navigation`, `linked_entities`, `build_references`, and
`analysis_surfaces`; all five are searched when the flag is omitted. Anything else fails with
`invalid_argument` (exit 2).

## Examples

```bash
icy-veins --pretty search "mistweaver monk guide" --limit 5
icy-veins resolve "fury warrior easy mode"
icy-veins guide mistweaver-monk-pve-healing-guide
icy-veins guide-export mistweaver-monk-pve-healing-guide --out ./tmp/mw-monk
icy-veins guide-query ./tmp/mw-monk "stat priority" --kind analysis_surfaces
```

## Output

Every command emits the shared envelope (`ok`, `provider`, `command`, `kind`, `schema_version`,
`query`, `provenance`, `data`, and `error` on failure) and no other top-level key; the payload is in
`data`.

Exit codes follow `docs/foundation/ERROR_CONTRACT.md`: 1 generic, 2 usage, 4 guide not found,
5 network/upstream failure. A page whose article container no longer matches (an Icy Veins layout
change) fails with `parse_failed` and exit 1 rather than returning an empty article with `ok:true`.

`guide-query` answers a bad bundle path the same way `method guide-query` does: a path that does
not exist is `not_found` (exit 4), a file is a usage error (exit 2), and a directory that is not a
readable bundle is `invalid_bundle` (exit 1): no `manifest.json`, a manifest whose `files` lists no
content file (`pages.jsonl`, `sections.jsonl`, `analysis-surfaces.jsonl`, ...), or a listed file that
is missing, corrupt, or holds a row with a wrongly typed nested field (a `build_identity` that is not
an object, `surface_tags` that is not a list). A `wowhead guide-export` bundle is readable; it has sections, navigation,
linked entities and analysis surfaces, but no pages or build references.

### Build references

`build_references` carries explicit build evidence from the page, never a guess from the slug or
title. Two reference types are emitted:

| `reference_type` | Source on the page | `url` |
| --- | --- | --- |
| `wowhead_talent_calc_url` | an embedded Wowhead talent-calc link | the talent-calc URL |
| `wow_talent_export` | a published WoW loadout import string (the `Copy` blocks on the talents pages) | the import string itself, because the reference has no link |

Both types set `build_code`, so `warcraft guide-builds-simc` collects either one and reports it
under `summary.identify_success_count`. Decoding needs a class and a spec, and the two types supply
them differently:

- `wowhead_talent_calc_url` always decodes unaided: its URL path names the class and spec.
- `wow_talent_export` names neither, so `build_identity` stays unknown on the row and SimC has to
  identify the string itself. It probes every spec in SimC's specialization data, healers included.

So `simc decode-build --talents <build_code>` returns `ok:true` for an Icy Veins build of any role
without `--actor-class` or `--spec` (verified against the Fury Warrior talents page: the probe returns
`warrior`/`fury` with `confidence: high`).

The Icy Veins builds/talents pages publish import strings rather than talent-calc links, so in
practice the rows you get back are `wow_talent_export`.

### Partial guide bundles

`guide-full` and `guide-export` walk every page in the family navigation. A page that cannot be
fetched or parsed is skipped rather than failing the whole bundle, and it is reported in
`data.failed_pages` (`{count, items: [{url, section_slug, error: {code, message}}]}`). Content from
those pages is missing from the merged sections, entities, build references, and analysis surfaces.

## Supported guide families

Sitemap discovery and `guide` only accept slugs that classify into a known family. Unclassified WoW
pages fail with `invalid_guide_ref`.

| Family | Example slug |
| --- | --- |
| `class_hub` | `monk-guide` |
| `role_guide` | `healing-guide` |
| `spec_guide` | `mistweaver-monk-pve-healing-guide` |
| `easy_mode` | `fury-warrior-pve-dps-easy-mode` |
| `leveling` | `mistweaver-monk-leveling-guide` |
| `pvp` | `mistweaver-monk-pvp-guide` |
| `spec_builds_talents` | `mistweaver-monk-pve-healing-spec-builds-talents` |
| `rotation_guide` | `mistweaver-monk-pve-healing-rotation-cooldowns-abilities` |
| `stat_priority` | `mistweaver-monk-pve-healing-stat-priority` |
| `gems_enchants_consumables` | `mistweaver-monk-pve-healing-gems-enchants-consumables` |
| `gear_best_in_slot` | `mistweaver-monk-pve-healing-gear-best-in-slot` |
| `spell_summary` | `mistweaver-monk-pve-healing-spell-summary` |
| `resources` | `mistweaver-monk-resources` |
| `mythic_plus_tips` | `mistweaver-monk-pve-healing-mythic-plus-tips` |
| `macros_addons` | `mistweaver-monk-pve-healing-macros-addons` |
| `simulations` | `mistweaver-monk-pve-healing-simulations` |
| `raid_guide` | `mistweaver-monk-pve-healing-nerub-ar-palace-raid-guide` |
| `expansion_guide` | `mistweaver-monk-the-war-within-pve-guide` |
| `special_event_guide` | `mistweaver-monk-mists-of-pandaria-remix-guide` |

`guide-full` traversal is family-aware: class hubs and role guides stay on the current page, and
every other family walks its own navigation block.

Patch notes, class-change roundups, hotfix posts, and news pages are out of scope. `search` and
`resolve` detect those query intents and return an empty result set with a `scope_hint` instead of
misleading guide matches.

`search` and `resolve` keep only guides whose name or slug contains a query word as a whole word, so
`dh` does not match "headhunters". A trailing plural `s` is ignored on both sides, so `build` keeps
the `...-spec-builds-talents` pages. Words such as `a`, `of` and `the` are ignored, and `+` reads as
`plus`, so `mythic+` finds the "Mythic Plus" pages. There are no class or spec abbreviations: `dk`
and `mw` match nothing.

## Caching

HTTP responses are cached through `warcraft_api.cache`. Sitemap XML and guide HTML are cached
separately.

| Variable | Default |
| --- | --- |
| `ICY_VEINS_CACHE_BACKEND` | `file` (`redis` also supported) |
| `ICY_VEINS_CACHE_DIR` | `<cache root>/icy-veins/http` |
| `ICY_VEINS_REDIS_URL` | unset |
| `ICY_VEINS_REDIS_PREFIX` | `icy_veins_cli` |
| `ICY_VEINS_SITEMAP_CACHE_TTL_SECONDS` | `86400` |
| `ICY_VEINS_PAGE_CACHE_TTL_SECONDS` | `3600` |

`icy-veins doctor` prints the resolved values.

## Tests

- `tests/test_icy_veins_cli.py` - parsing, ranking, command contracts, transport error envelopes
- `tests/test_icy_veins_recorded_fixtures.py` - captured real pages (pre-redesign and Astro layouts)
  plus the slug-to-family classification table
- `tests/e2e/test_icy_veins.py` - live end-to-end journeys, run with `make test-e2e E2E_ARGS="tests/e2e/test_icy_veins.py"`

## Not in scope

Login/premium content, non-WoW Icy Veins games, news ingestion, and patch-analysis pages.

## Source links

- `https://www.icy-veins.com/sitemap.xml`
- `https://www.icy-veins.com/wow/monk-guide`
- `https://www.icy-veins.com/wow/healing-guide`
- `https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide`
- [Roadmap](../ROADMAP.md)

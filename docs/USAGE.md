# Usage

This document carries workflows and cross-provider conventions: what each surface is for, when to
reach for it, and how the pieces compose.

It does not list flags. Every command, argument, option, and default is generated from the Typer
apps into [reference/](reference/README.md) (`make reference`), so the flag lists cannot drift from
the code. Use this page to decide *what* to run and the reference to see *how* to spell it.

## Workspace Commands

```bash
uv sync --all-extras
make check
make test-e2e
make reference
```

Workspace command behavior:
- `uv sync --all-extras` (or `make install`) creates and updates the checkout-local `.venv`; `pip install -e '.[dev,redis]'` still works
- `make check` runs lint, typecheck, import boundaries, the complexity gate, dead-code detection, and the fast test suite with its coverage floor
- `make test-e2e` runs the end-to-end journeys through the installed binaries against the real providers ([E2E_TESTING.md](architecture/E2E_TESTING.md)); `make test-canary` runs the live Wowhead parser canary (`WOWHEAD_LIVE_TESTS=1`)
- `make reference` regenerates `docs/reference/`; `make skills` regenerates the generated provider subskills. Neither output is hand-edited
- `make dev-deploy-no-link` refreshes the checkout-local editable environment without rewriting host-level command wrappers; `make worktree-env` regenerates `.warcraft/worktree-env.sh`, and `source .warcraft/worktree-env.sh` activates worktree-local `PATH`, data, and cache roots
- `WARCRAFT_ALLOW_LINK_BIN=1 make dev-deploy` is a deliberate exception that repoints `~/.local/bin` at the current checkout

## Global Flags

These flags exist on every binary — the `warcraft` wrapper and all eleven providers — and go
**before** the subcommand. Full contract: [foundation/ERROR_CONTRACT.md](foundation/ERROR_CONTRACT.md).

| Flag | Effect |
|------|--------|
| `--pretty` | Pretty-print JSON. Default output is compact JSON for machine consumption. |
| `--compact` | Truncate long prose strings (tooltip HTML, article text) and list each cut path in `provenance.compacted_paths`; URLs, talent/transport strings, export codes and `*command` values stay whole. |
| `--compact-max-chars` | Truncation length for `--compact` (default `280`). |
| `--fields` | Keep only the listed dot paths; repeatable or comma-separated. |
| `--fields-strict` | Fail when a requested `--fields` path is missing (`missing_fields`, exit 2). Without it, missing paths are listed in the projected payload under `fields_missing`, so a thin projection is never mistaken for an empty result. |
| `--profile` | `agent` (default compact JSON) or `human` (pretty JSON). |

Some binaries add their own global flags: `warcraft` and `wowhead` take `--expansion` (a version
profile; the wrapper passes it through to expansion-aware providers), `warcraftlogs` takes
`--site retail|classic|fresh`, and `simc` takes `--repo-root`. The per-binary lists are in
[reference/](reference/README.md).

```bash
wowhead --fields query,data.count,data.results search "defias"
warcraft --expansion wotlk search "thunderfury"
```

Every provider command emits one JSON object with the shared envelope keys `ok`, `provider`,
`command`, `kind`, `schema_version`, `query`, `provenance`, and `data`; failures add `error` with a
stable `code`, a human `message`, and optional `details`, and are written to stderr. Read the payload
from `data`. The `warcraft` wrapper's own commands (`doctor`, `search`, `resolve`, and the composite
packets) emit exactly those envelope keys with `provider: "warcraft"` (`resolve` reports the provider
it selected as `data.selected_provider`), and every failure has `kind: "error"`: everything else is
under `data` on success and under `error.details` on failure.
`warcraft <provider> ...` passthrough output is the provider's envelope.

Exit codes:

| Exit | Meaning |
|------|---------|
| `0` | success |
| `1` | generic failure, including uncaught exceptions |
| `2` | usage error: bad flags or arguments |
| `3` | authentication required or rejected |
| `4` | target not found |
| `5` | network or upstream failure |

## Wrapper Commands

Flags: [reference/warcraft.md](reference/warcraft.md). Provider docs: [warcraft/README.md](warcraft/README.md).

```bash
warcraft doctor
warcraft schema
warcraft search "defias"
warcraft --expansion wotlk search "thunderfury" --brief --expansion-debug
warcraft resolve "fairbreeze favors"
warcraft resolve "https://www.warcraftlogs.com/reports/abcd1234#fight=3"
warcraft guild us "Mal'Ganis" gn
warcraft guide-compare-query "mistweaver monk guide"
warcraft guide-compare ./tmp/method-mistweaver ./tmp/icy-mistweaver
warcraft guide-builds-simc ./tmp/method-mistweaver --apl-path <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc
warcraft cooldown-packet "https://www.warcraftlogs.com/reports/abcd1234#fight=3" --actor-id 1234 --phase 2
warcraft talent-packet https://www.warcraftlogs.com/reports/abcd1234#fight=47 --actor-id 1234
warcraft talent-describe druid/balance/ABC123 --apl-path <simc-root>/ActionPriorityLists/default/druid_balance.simc
warcraft --expansion wotlk wowhead search "thunderfury"
warcraft wowhead guide 3143
warcraft simc analysis-packet <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc --targets 1
```

## Wrapper Conventions

- Use `warcraft` when the provider is unclear.
- Put global flags before the subcommand, for example `warcraft --pretty search "defias"` or `warcraft --expansion wotlk search "thunderfury"`.
- Use `warcraft resolve` for a conservative next-command recommendation across providers.
- Use `warcraft search` when you want to inspect candidates across providers.
- Use `warcraft <provider> ...` when you already know which service you need.
- Use `warcraft --expansion <profile>` when the game version matters and you do not want silent cross-version mixing.
- `method` is a guide provider with sitemap-backed search, resolve, export, and local query.
- `icy-veins` is a guide provider with sitemap-backed search, resolve, export, and local query.
- `raiderio` is an API provider for direct character, guild, and Mythic+ runs lookups.
- `raiderio` includes real search and conservative resolve on top of the live site search surface.
- `warcraft-wiki` is a reference provider with MediaWiki-backed search, resolve, typed `api` / `event` lookups, article export, and local query.
- `warcraftlogs` is an official API provider with OAuth client-credentials auth plus typed world metadata, guild, character, and report lookups; `--site retail|classic|fresh` selects the site profile and the wrapper maps `--expansion` onto it.
- `warcraftlogs` is wired into wrapper `doctor`, passthrough, and conservative wrapper `search` / `resolve`.
- wrapper discovery for `warcraftlogs` is intentionally narrow: only explicit report URLs and bare report codes resolve through the wrapper. A bare code is any 16 letters and digits, or 8 to 32 letters and digits with at least one digit, so a 16-letter single word (`restorationdruid`) is read as a report code too.
- `lorrgs` is a no-auth Lorrgs public API provider for top-parse cooldown timelines, composition rankings, static spec/boss/spell metadata, and Warcraft Logs report overview handoffs; wrapper `search`/`resolve` understand Lorrgs URLs, Warcraft Logs report URLs, bare report codes, and spec/boss text.
- `lorrgs` is fixed to retail for wrapper expansion eligibility because it exposes current Warcraft Logs-derived raid ranking data and has no classic/fresh selector.
- `warcraft cooldown-packet` is the cross-provider packet for user-specific phase cooldown questions:
  - it uses cached Lorrgs user-report fight data for phase markers, boss casts, boss/spec slugs, and player source ids
  - it uses Warcraft Logs `report-events --data-type casts` for the selected player's exact cast timestamps
  - it uses Lorrgs `spec-spells`, `boss-spells`, and optional `spec-ranking` samples to label cooldowns and compare top-parse phase timing. The comparison uses `--difficulty` when passed, otherwise the Warcraft Logs fight's own difficulty (heroic or mythic, echoed as `query.difficulty`); a fight at any other difficulty gets no comparison and a note saying why
  - it emits phase windows, selected-phase player casts, selected-phase boss casts, tracked spell metadata, top-parse samples, source commands, and notes; it does not synthesize strategy advice
  - the top-parse comparison needs a Lorrgs boss slug, which only a Lorrgs-cached report supplies; for any other report pass `--boss-slug`. When the comparison does not run, `comparison.reason` names why and a note names the flag that fixes it
  - Lorrgs only serves reports it has already cached. For any other report — or when Lorrgs itself is unreachable — pass `--actor-id` and `--spec-slug` and the command degrades instead of failing: the packet still carries the Warcraft Logs cast timeline, with `lorrgs.status: "unavailable"` and `phase.status: "unavailable"`. `lorrgs.message` names the real reason (only a `not_found` is reported as "not cached"; a timeout or transport failure says so) and `lorrgs.source` keeps the provider's own error
  - in that degraded mode there are no phase windows, so `--phase` cannot be applied: `phase.requested` echoes the phase you asked for, `phase.selected` is `null`, `cooldowns.player_casts` covers the whole fight, and `notes` says so. Without `--actor-id` and `--spec-slug` the command fails instead, naming the Lorrgs error and the two flags
- `warcraft guild` normalizes region/realm/name input and returns the Raider.IO guild snapshot (identity, raid progression, roster preview, citations) with the provider-native payload preserved under `sources.raiderio`. There is no `active_raid`: `sources.raiderio.summary.raids[]` carries every raid Raider.IO returned, each joined to its own ranks by `raid_slug`. Raider.IO orders those rows by slug and carries no raid start/end window, so naming one "active" would be a guess — cross-reference `raiderio raids`, whose rows carry per-region `starts`/`ends`, when you need the currently running tier.
- `warcraft guide-compare` compares exported guide bundles across providers using raw section evidence, additive `analysis_surfaces`, and explicit `build_references`, while preserving provider provenance and source citations instead of flattening the guides into one fake summary
- `guide-compare` also emits a top-level `freshness` rollup and a `comparison_evidence` block (compared bundle count, providers, matching rules, and per-bundle freshness from each bundle's `exported_at`); `--max-age-hours` (default `24`) sets the per-bundle freshness threshold. Method/Icy Veins/Warcraft Wiki bundles carry an `exported_at` timestamp in their manifest; older bundles without it degrade to `stale`/`missing_exported_at` rather than failing
- `warcraft guide-compare-query` conservatively resolves one guide per supported provider, exports those bundles locally, and then runs the same comparison packet over the exported evidence
- when `guide-compare-query` cannot get a guide from provider `resolve`, it may fall back to provider `search`, but only when the top guide result has a strong enough score and a clearly decisive lead over the alternatives; weak or ambiguous guide search results are skipped instead of exported
- when every provider that contributed no bundle failed outright, `guide-compare-query` fails with those providers' shared error code and exit code (a network outage exits 5) instead of `insufficient_guides`, and each `provider_results` row carries its `error`
- `guide-compare-query` writes an orchestration manifest under the output root and reuses existing bundles only when the same guide ref is still selected and the recorded export age is within `--max-age-hours`; use `--force-refresh` to bypass reuse
- `guide-compare-query` orchestration controls: `--provider` (repeatable) restricts the run to `wowhead`, `method`, or `icy-veins`; `--out-root` sets where the exported bundles and the orchestration manifest are written; `--max-age-hours` and `--force-refresh` control bundle reuse
- `guide-compare-query --simc-build-handoff` adds an explicit guide-build-to-`simc` evidence block derived only from exported `build_references`; use `--simc-apl-path` when you also want exact-build `simc describe-build` output in the same packet, `--simc-decode` / `--no-simc-decode` to control whether each explicit build ref is also run through `simc decode-build`, and `--simc-build-limit` to cap how many unique build refs are handed off
- `warcraft guide-builds-simc` reads explicit embedded guide build references from one exported guide bundle or a `guide-compare-query` output root, dedupes them, and hands each one to `simc identify-build` (plus optional `simc decode-build`) in the form its reference type requires: a published `wow_talent_export` string as `--build-text`, a Wowhead talent-calc URL as a transport packet. A requested leg that produced nothing at all for any build reports `summary.simc_handoff_status: "failed"` with `summary.empty_requested_legs` naming the leg — never `ok` or `partial` next to a zero counter — and each build's own `failures` carry the simc error code that caused it. When every requested leg produced nothing, the command fails with `simc_handoff_failed` (exit 1) and the packet under `error.details`
- `warcraft guide-builds-simc --apl-path <apl>` also runs `simc describe-build` for each explicit build ref so the handoff can include exact-build APL-backed detail without inferring claims from guide prose
- `guide-builds-simc` also includes explicit provenance, citations, and source freshness metadata for the handoff packet so agents can tell whether the build evidence came from one bundle or a fresher orchestration root; for a single bundle the freshness `status` is now `known` (anchored on the bundle's `exported_at`) rather than `unknown` when the manifest carries an export timestamp
- each handed-off guide build now also carries an exact `talent_transport_packet`, so explicit Wowhead build refs and raw Warcraft Logs talent trees can travel through the same packet contract
- `warcraft talent-packet` is the wrapper-level packet router:
  - explicit Wowhead talent-calc refs with build codes route to `wowhead talent-calc-packet`
  - explicit Warcraft Logs report refs with `--actor-id` route to `warcraftlogs report-player-talents`, but you still need encounter scope via `--fight-id` or a report ref already scoped to one fight; those packets can then auto-upgrade through `simc`
  - existing packet JSON files can be re-emitted or upgraded without choosing a provider first
- `warcraft talent-describe` reuses that same routing contract, then hands the final packet to `simc describe-build`
  - use `--packet-out <path>` when you want to keep the final routed packet that was described, including any validation upgrade
  - use `--apl-path <apl>` when you want to pin the SimC APL instead of relying on default APL inference
- end-to-end packet flow:
  - `warcraftlogs report-player-talents abcdefgh --fight-id 47 --actor-id 1234 --out ./tmp/gubkfc-packet.json`
  - `simc validate-talent-transport --build-packet ./tmp/gubkfc-packet.json --out ./tmp/gubkfc-packet-validated.json`
  - `warcraft talent-describe ./tmp/gubkfc-packet-validated.json --apl-path <simc-root>/ActionPriorityLists/default/druid_balance.simc`
- malformed-packet troubleshooting:
  - packet producers now fail with `invalid_transport_packet` before they print or write malformed packet JSON
  - malformed Wowhead-like talent refs, including exact packet refs without a build code, now fail with `invalid_tool_ref`
  - wrapper routes preserve that same `invalid_transport_packet` error instead of hiding it behind a generic wrapper failure
  - `simc identify-build`, `simc decode-build`, `simc describe-build`, and `simc validate-talent-transport` fail with `invalid_build_packet` when `--build-packet` points at malformed packet JSON
- `simc` is a local-tool provider for local repo inspection, build decoding, and binary execution.
- `simc` includes readonly analysis commands for APL list inspection, graphing, talent gates, and action tracing.
- `simc` includes conservative prune, branch-trace, and intent analysis.
- `simc` includes comparison, packet, first-cast, and log-actions commands built on the same conservative reasoning layer.
- wrapper `search` and `resolve` fan out only to providers whose wrapper routing surfaces are currently ready; stubbed surfaces such as `simc` remain visible in `warcraft doctor` and excluded-provider metadata instead of appearing as active wrapper candidates
- the merged `warcraft search` list interleaves the providers' own lists using a tunable wrapper ranking layer that combines provider score, query intent, provider family, and result kind; it never reorders two rows from the same provider, because a provider's own order is its ranking
- flattened wrapper results include `wrapper_ranking` so agents can inspect why a provider/result surfaced first
- `warcraft resolve` ranks each provider's match exactly as `warcraft search` ranks that provider's top row, and answers with the top-ranked one only when its own provider resolved it and the query's intent does not rank its family down; otherwise it reports `resolved: false` with that candidate as `best_unresolved_candidate`. Its `--limit` only sizes `--ranking-debug`
- `warcraft search --brief` and `warcraft resolve --brief` shrink candidate rows to the wrapper decision surface and drop the per-provider payloads; `--compact` is the global output flag only (before the subcommand) and truncates long prose strings in any payload. The two no longer share a name: `--compact` after the subcommand is a usage error (exit 2)
- `--brief` never hides a provider failure: `failed_providers`, `failed_provider_count`, and `answered_provider_count` stay in both shapes
- `warcraft search` reports `count` as the merged candidate total and `truncated` as whether `--limit` cut the list; each provider's scores are rescaled against that provider's own best row before the merge, with a floor so a provider whose best row is weak is not promoted for topping its own empty field
- the merged page then applies three rules, all visible in the payload (see `docs/foundation/WRAPPER_PROVIDER_CONTRACT.md`): a bare name is not a profile query, so Raider.IO rows are marked `wrapper_ranking.off_intent` and rank below every row from a family the query asked for, while the entity provider's own top row anchors the page when its title is, or starts with, the query (with no anchor, one slot stays reserved when Raider.IO's first row is named exactly the query); no single provider may take more than half the page (over-cap rows are deferred and fill the remaining slots, off-intent rows only fill what is left, up to a strict minority); and between providers a row whose own title is, or starts with, the query outranks one that merely mentions it. A guide row the provider flagged as superseded carries `wrapper_ranking.stale_guide: true`, in the `--brief` rows too. `data.merge_policy` reports the caps, the reserved slot, and how many rows they deferred or withheld
- use `--ranking-debug` when you want compact ranking summaries for the top wrapper candidates
- use `--expansion-debug` when you want a compact per-provider expansion eligibility snapshot
- wrapper ranking policy can be overridden with `~/.config/warcraft/wrapper_ranking.json`
- wrapper expansion filtering is conservative:
  - `wowhead` and `warcraftlogs` are the profiled expansion-aware providers; `warcraftlogs` maps the requested expansion onto its `retail` / `classic` / `fresh` site profile and rejects keys it cannot honor (`ptr`, `beta`, `classic-ptr`)
  - `method`, `icy-veins`, `raiderio`, `lorrgs`, and `raidbots` are treated as retail-only when wrapper expansion filtering is active
  - `warcraft-wiki`, `simc`, `blizzard`, and `curseforge` are excluded from wrapper expansion-filtered `search` and `resolve`
- wrapper `search`, `resolve`, and `doctor` report included and excluded providers when expansion filtering is active
- wrapper `doctor` also reports wrapper-surface readiness plus provider auth/install metadata, so agents can distinguish a registered provider from a wrapper-ready routing surface
- `--expansion-debug` exposes the full provider eligibility snapshot even in compact mode
- direct passthrough commands reject unsupported provider/expansion combinations instead of silently ignoring the expansion request
- wrapper `doctor` preserves provider registration status, so partial providers stay marked `partial` even when their local doctor command succeeds
- wrapper `doctor` reports `wrapper.tiers` plus a `tier` on every provider row (core / supported / experimental), and answers fully offline: it reads local auth and runtime state instead of probing provider endpoints
- provider rows in `warcraft search` and `warcraft resolve` carry `ok` and `error` alongside `status`: `status` is registry readiness, `ok`/`error` is what the call actually did, so a provider failure is an error envelope instead of a null payload
- the wrapper calls each provider's in-process `PROVIDER` surface for `search`, `resolve`, and `doctor`, and invokes the provider's own app for `warcraft <provider> ...` passthrough; global output flags are forwarded to the provider on passthrough

`warcraft doctor` reports:
- wrapper health
- provider tiers and registered provider readiness
- effective XDG-style config/data/cache/state roots
- active worktree-runtime isolation details when running from an editable worktree
- provider expansion-support mode and active expansion eligibility when `--expansion` is set

The product philosophy behind these surfaces (what the repo will and will not answer) lives in
[foundation/PRODUCT_PRINCIPLES.md](foundation/PRODUCT_PRINCIPLES.md).

## Provider Commands

Each provider documents its own commands, examples and caveats; the generated reference lists every flag.

- `wowhead`: [wowhead/README.md](wowhead/README.md), flags in [reference/wowhead.md](reference/wowhead.md)
- `method`: [method/README.md](method/README.md), flags in [reference/method.md](reference/method.md)
- `icy-veins`: [icy-veins/README.md](icy-veins/README.md), flags in [reference/icy-veins.md](reference/icy-veins.md)
- `raiderio`: [raiderio/README.md](raiderio/README.md), flags in [reference/raiderio.md](reference/raiderio.md)
- `warcraft-wiki`: [warcraft-wiki/README.md](warcraft-wiki/README.md), flags in [reference/warcraft-wiki.md](reference/warcraft-wiki.md)
- `lorrgs`: [lorrgs/README.md](lorrgs/README.md), flags in [reference/lorrgs.md](reference/lorrgs.md)
- `warcraftlogs`: [warcraftlogs/README.md](warcraftlogs/README.md), flags in [reference/warcraftlogs.md](reference/warcraftlogs.md)
- `simc`: [simc/README.md](simc/README.md), flags in [reference/simc.md](reference/simc.md)
- `raidbots`: [raidbots/README.md](raidbots/README.md), flags in [reference/raidbots.md](reference/raidbots.md)
- `blizzard`: [blizzard-api/README.md](blizzard-api/README.md), flags in [reference/blizzard.md](reference/blizzard.md)
- `curseforge`: [curseforge/README.md](curseforge/README.md), flags in [reference/curseforge.md](reference/curseforge.md)

## Output Conventions

- The envelope, the exit codes, and the shared output flags are described once under [Global Flags](#global-flags) and in full in [foundation/ERROR_CONTRACT.md](foundation/ERROR_CONTRACT.md).
- `warcraft schema` prints that envelope as a draft 2020-12 JSON Schema; the same document is checked in at [../schemas/envelope.schema.json](../schemas/envelope.schema.json) for tooling that cannot run the CLI.
- Provider responses carry `ok: true` on success; structured failures on every binary carry `ok: false` plus an `error` object on stderr.
- `--citation-pack` on Wowhead commands attaches a deterministic `citation_pack` with source URLs and per-claim anchors (`entity`, `compare`, and similar payloads).
- Wrapper responses preserve provider provenance instead of flattening everything into a fake universal schema.

The remaining sections go deeper on the Wowhead surfaces (routing, entity retrieval, bundles,
querying, compare, and cache), because they carry the most workflow-specific behavior.

## Expansion And Routing

- Use global `--expansion` to target a version profile; default is `retail`.
- Use `--normalize-canonical-to-expansion` if you want canonical entity page URLs forced into the selected expansion path.
- Some entity types use special routing under the hood:
  - `faction` and `pet` use page-metadata tooltip fallbacks
  - `recipe` resolves through spell pages
  - `mount` resolves through underlying item pages
  - `battle-pet` resolves through underlying NPC pages

## Entity Retrieval

- `entity` is the compact main retrieval command.
- `entity-page` is the richer page exploration command.
- `comments` is the comment-focused command.
- `comments` filters on date range, reply count, author, and keyword, and `--insights` adds sample-backed freshness, near-duplicate groups, and cited top insights (no sentiment scores).

Regular `entity`, `guide`, and `comments` responses include a lightweight `linked_entities` preview with:
- basic records
- `counts_by_type`
- `fetch_more_command`

Use `--linked-entity-preview-limit 0` on `entity` or `comments` if you want to skip that preview.

`entity` responses expose:
- `entity.name`
- `entity.page_url`
- `tooltip.summary`
- `tooltip.text`
- `tooltip.html`

When comments are included:
- `citations.comments` is the comment-thread source URL
- `comments.needs_raw_fetch` indicates whether a larger raw comments fetch is still useful

Tooltip cleanup behavior:
- item and mount summaries prefer actionable effect or use text over boilerplate
- spell summaries prefer the descriptive effect clause over cast metadata
- cleaned tooltip text normalizes noisy spacing, money strings, and long flavor-text noise

## Guides And Bundles

- `guide` resolves guide IDs or URLs and returns metadata plus sampled comments.
- `guide` also exposes additive `analysis_surfaces` derived from trusted guide-body section headings and preserves raw guide detail alongside them.
- `guide-full` returns the rich embedded guide payload in one response.
- `guide-export` writes local guide assets for repeated agent exploration.

`guide-export` writes files such as:
- `guide.json`
- `page.html`
- `sections.jsonl`
- `analysis-surfaces.jsonl`
- `linked-entities.jsonl`
- `comments.jsonl`
- `manifest.json`

Optional hydration:
- `--hydrate-linked-entities` writes compact local entity payloads under `entities/<type>/<id>.json`
- default hydrated types are `spell,item,npc`
- use `--hydrate-type` and `--hydrate-limit` to narrow the hydration set

Hydration behavior:
- it reuses the normalized `entity` contract
- it checks the normalized entity cache before live fetches
- hydrated entity manifests track `stored_at` and `storage_source`
- bundle manifests expose `hydration.source_counts`

Search and resolve:
- use `search` when you want to browse candidates or the query is likely ambiguous
- `search` results include `ranking` plus `follow_up`, so each candidate carries a suggested next command such as `entity`, `entity-page`, `guide`, `guide-full`, or `comments`
- when the query contains follow-up words like `comments`, `links`, or `full`, the CLI strips those from the upstream Wowhead lookup and exposes the actual request text as `search_query`; a query that is itself a name made of such words ("Soul Link") is searched whole first and kept when a row carries exactly that name, and `search` given a Wowhead entity URL answers with that entity without searching
- use `resolve` when you want the CLI to choose the best next command conservatively
- `resolve` reuses the same follow-up guidance, but only emits `next_command` when confidence is high
- `resolve --entity-type guide` or similar can safely narrow ambiguous queries when the caller already knows the target class of thing; Wowhead's own guide ranking counts when the query says `guide` or `--entity-type guide` is set

Bundle discovery and refresh:
- bundle freshness summaries include reason fields such as `bundle_reasons` and `hydration_reasons`, so stale bundles can be triaged without opening the manifest
- `guide-bundle-list`, `guide-bundle-search`, and `guide-bundle-query` expose root-level `stale_reason_counts` rollups
- `guide-bundle-inspect --summary` returns a compact trust-check payload focused on freshness and issues
- `guide-bundle-list` discovers bundles under `./wowhead_exports/` or another root
- `guide-bundle-search` searches indexed bundle metadata across a root
- `guide-bundle-query` searches exported bundle content across a root using the same match kinds and linked-source filters as `guide-query`
- `guide-bundle-inspect` checks one bundle for freshness, file presence, observed counts, and root index membership
- `guide-bundle-index-rebuild` rescans a root and rewrites `index.json` explicitly for repair cases
- it includes `freshness` and `hydration` summaries
- `--max-age-hours` changes the freshness threshold used by those summaries
- bundle exports and refreshes maintain a root-level `index.json`
- `guide-bundle-list`, `guide-bundle-search`, and `guide-bundle-query` prefer that index when it is present and valid
- `guide-bundle-refresh` refreshes an existing bundle in place
- `cache-inspect` shows current cache config plus namespace-level stats for the active file or Redis backend
- `cache-clear` clears cache entries across all namespaces or selected namespaces, with `--expired-only` support for file-backed caches
- `search` reranks upstream suggestions locally and includes lightweight `ranking.score` plus `ranking.match_reasons` per result
- `resolve` is the conservative one-shot discovery path: it picks a best match and returns a runnable `next_command` only when confidence is high, otherwise it falls back to `search`
- if `--max-age-hours` is omitted on refresh, the default freshness window is `24`
- refresh selectively rehydrates stale hydrated entity payloads unless `--force` is used

## Guide Querying

`guide-query` searches one exported guide bundle locally across:
- section content
- additive analysis surfaces
- navigation links
- linked entities
- gatherer entities
- comments

It accepts either:
- a direct bundle path
- a selector such as guide ID under `--root`

Results can be narrowed by match kind, section title, and linked-entity source (`href`, `gatherer`, or `multi`).

The flattened `top` list prefers merged linked-entity rows over duplicate raw gatherer rows for the same entity.

## Compare

`compare` performs multi-entity analysis with:
- normalized summary fields
- linked-entity overlap and unique sets
- comment context
- canonical citation links
- `--preset gear|quest|spell` to tune comparable fields, link limits, and comment sampling (explicit flags override preset defaults)

Generated overlap and unique linked-entity rows use a single canonical `url`.

## Cache

Transport caching is configurable through env vars. Useful defaults:

```bash
WOWHEAD_CACHE_BACKEND=file
WOWHEAD_CACHE_DIR=~/.cache/warcraft/wowhead/http
WOWHEAD_SEARCH_CACHE_TTL_SECONDS=900
WOWHEAD_TOOLTIP_CACHE_TTL_SECONDS=3600
WOWHEAD_ENTITY_PAGE_CACHE_TTL_SECONDS=3600
WOWHEAD_GUIDE_PAGE_CACHE_TTL_SECONDS=3600
WOWHEAD_COMMENT_REPLIES_CACHE_TTL_SECONDS=1800
WOWHEAD_ENTITY_CACHE_TTL_SECONDS=3600
```

Optional Redis support:

```bash
WOWHEAD_CACHE_BACKEND=redis
WOWHEAD_REDIS_URL=redis://host:6379/3
WOWHEAD_REDIS_PREFIX=wowhead_cli
```

Current active cache layers:
- transport cache for raw tooltip, page, search, and comment responses
- normalized `entity` response cache for repeated `entity` lookups with the same flags

The normalized entity cache is expansion-scoped.

Redis visibility:
- `cache-inspect --show-redis-prefixes` adds a bounded `prefix_visibility` summary for shared Redis deployments
- it shows whether the configured prefix appears isolated, how many keys live under other prefixes, and a capped list of visible prefixes in the same Redis

Cache cleanup and compact inspection:
- `cache-inspect --summary` returns a compact top-namespace view instead of the full namespace listing
- `cache-inspect --hide-zero` removes zero-valued count fields from cache stats
- summary-mode file cache inspection includes `age_summary` with oldest/newest entry timestamps and ages
- `cache-repair` reports legacy unscoped file-cache entries; `cache-repair --apply` prunes them
- `cache-repair --expired-only` limits that repair to expired legacy entries

## Related Docs

- [reference/README.md](reference/README.md) — generated per-command flag reference
- [foundation/ERROR_CONTRACT.md](foundation/ERROR_CONTRACT.md) — envelope, error codes, exit codes
- [foundation/PRODUCT_PRINCIPLES.md](foundation/PRODUCT_PRINCIPLES.md)
- [ROADMAP.md](ROADMAP.md)

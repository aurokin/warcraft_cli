# Usage

This document is the command-oriented reference for the local Warcraft CLI workspace.

The goal is to keep the README short and keep detailed usage notes close to actual CLI behavior.

## Workspace Commands

```bash
make dev-deploy-no-link
make worktree-env
source .warcraft/worktree-env.sh
WARCRAFT_ALLOW_LINK_BIN=1 make dev-deploy
```

Workspace command behavior:
- `make dev-deploy-no-link` creates or updates the current checkout's editable `.venv` without rewriting host-level command wrappers
- `make worktree-env` regenerates `.warcraft/worktree-env.sh` for the current checkout when you want to refresh the shell activation file explicitly
- `source .warcraft/worktree-env.sh` is the explicit shell activation step for worktree-local `PATH`, `data`, and `cache` roots
- `WARCRAFT_ALLOW_LINK_BIN=1 make dev-deploy` is a deliberate exception that repoints `~/.local/bin` at the current checkout
- worktree creation and trunk hygiene are handled outside this repo with `worktrunk`

## Wrapper Commands

```bash
warcraft doctor
warcraft --expansion wotlk doctor
warcraft search "defias"
warcraft --expansion wotlk search "thunderfury"
warcraft --expansion wotlk search "thunderfury" --compact --expansion-debug
warcraft search "guild us illidan Liquid" --compact --ranking-debug
warcraft resolve "fairbreeze favors"
warcraft --expansion wotlk resolve "thunderfury"
warcraft --expansion wotlk resolve "guild us illidan Liquid" --compact --expansion-debug
warcraft resolve "character us illidan Roguecane" --compact --ranking-debug
warcraft guild us "Mal'Ganis" gn
warcraft guild-history us "Mal'Ganis" gn
warcraft guild-ranks us "Mal'Ganis" gn
warcraft guide-compare ./tmp/method-mistweaver ./tmp/icy-mistweaver
warcraft guide-compare-query "mistweaver monk guide"
warcraft guide-compare-query "mistweaver monk guide" --simc-build-handoff --simc-apl-path <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc
warcraft guide-builds-simc ./tmp/method-mistweaver
warcraft guide-builds-simc ./tmp/method-mistweaver --apl-path <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc
warcraft talent-packet druid/balance/ABC123
warcraft talent-packet https://www.warcraftlogs.com/reports/abcd1234#fight=47 --actor-id 1234
warcraft talent-packet ./tmp/gubkfc-packet.json --out ./tmp/gubkfc-packet-validated.json
warcraft talent-describe druid/balance/ABC123 --apl-path <simc-root>/ActionPriorityLists/default/druid_balance.simc
warcraft --expansion wotlk wowhead search "thunderfury"
warcraft wowhead search "defias"
warcraft wowhead guide 3143
warcraft method search "mistweaver monk"
warcraft method guide mistweaver-monk
warcraft icy-veins search "mistweaver monk guide"
warcraft icy-veins guide mistweaver-monk-pve-healing-guide
warcraft raiderio character us illidan Roguecane
warcraft raiderio guild us illidan Liquid
warcraft warcraft-wiki search "world of warcraft api"
warcraft warcraft-wiki article "World of Warcraft API"
warcraft warcraft-wiki api "CreateFrame"
warcraft warcraft-wiki event "OnKeyDown"
warcraft wowprogress guild us illidan Liquid
warcraft wowprogress leaderboard pve us --limit 10
warcraft resolve "https://www.warcraftlogs.com/reports/abcd1234#fight=3"
warcraft warcraftlogs resolve "https://www.warcraftlogs.com/reports/abcd1234#fight=3"
warcraftlogs doctor
warcraftlogs auth status
warcraftlogs auth login --redirect-uri http://127.0.0.1:8787/callback
warcraftlogs auth pkce-login --redirect-uri http://127.0.0.1:8787/callback
warcraftlogs regions
warcraftlogs guild us illidan Liquid
warcraft simc doctor
warcraft simc version
warcraft simc spec-files mistweaver
warcraft simc apl-lists <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc
warcraft simc apl-intent <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc --targets 1
warcraft simc analysis-packet <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc --targets 1
warcraft simc first-cast <simc-root>/profiles/MID1/MID1_Monk_Windwalker.simc tiger_palm --seeds 1 --max-time 20
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
- `raiderio` is a phase-1 API provider for direct character, guild, and Mythic+ runs lookups.
- `raiderio` includes real search and conservative resolve on top of the live site search surface.
- `warcraft-wiki` is a reference provider with MediaWiki-backed search, resolve, typed `api` / `event` lookups, article export, and local query.
- `wowprogress` is a rankings provider with structured search, conservative resolve, direct guild/character/PvE leaderboard lookups, and sample-backed leaderboard analytics primitives.
- `warcraftlogs` is a phase-1 official API provider with retail-only OAuth client-credentials auth plus typed world metadata, guild, character, and report lookups.
- `warcraftlogs` is wired into wrapper `doctor`, passthrough, and conservative wrapper `search` / `resolve`.
- wrapper discovery for `warcraftlogs` is intentionally narrow: only explicit report URLs and bare mixed-alphanumeric report codes resolve through the wrapper.
- `warcraft guild` is a first-class merged guild workflow that normalizes region/realm/name input, preserves the provider-native Raider.IO and WowProgress payloads under each source, and reports explicit source disagreements as an additive wrapper layer.
- `warcraft guild-history` and `warcraft guild-ranks` currently route through the WowProgress provider surface and preserve the wrapped provider payload alongside the wrapper summary.
- `warcraft guide-compare` compares exported guide bundles across providers using raw section evidence, additive `analysis_surfaces`, and explicit `build_references`, while preserving provider provenance and source citations instead of flattening the guides into one fake summary
- `warcraft guide-compare-query` conservatively resolves one guide per supported provider, exports those bundles locally, and then runs the same comparison packet over the exported evidence
- when `guide-compare-query` cannot get a guide from provider `resolve`, it may fall back to provider `search`, but only when the top guide result has a strong enough score and a clearly decisive lead over the alternatives; weak or ambiguous guide search results are skipped instead of exported
- `guide-compare-query` writes an orchestration manifest under the output root and reuses existing bundles only when the same guide ref is still selected and the recorded export age is within `--max-age-hours`; use `--force-refresh` to bypass reuse
- `guide-compare-query --simc-build-handoff` adds an explicit guide-build-to-`simc` evidence block derived only from exported `build_references`; use `--simc-apl-path` when you also want exact-build `simc describe-build` output in the same packet
- `warcraft guide-builds-simc` reads explicit embedded guide build references from one exported guide bundle or a `guide-compare-query` output root, dedupes them, and hands those exact build refs to `simc identify-build` plus optional `simc decode-build`
- `warcraft guide-builds-simc --apl-path <apl>` also runs `simc describe-build` for each explicit build ref so the handoff can include exact-build APL-backed detail without inferring claims from guide prose
- `guide-builds-simc` also includes explicit provenance, citations, and source freshness metadata for the handoff packet so agents can tell whether the build evidence came from one bundle or a fresher orchestration root
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
- `simc` is a phase-1 local-tool provider for local repo inspection, build decoding, and binary execution.
- `simc` includes readonly analysis commands for APL list inspection, graphing, talent gates, and action tracing.
- `simc` includes an early phase-3 slice for conservative prune, branch-trace, and intent analysis.
- `simc` includes comparison, packet, first-cast, and log-actions commands built on the same conservative reasoning layer.
- `simc` search and resolve currently return structured `coming_soon` payloads in phase 1.
- wrapper `search` and `resolve` fan out only to providers whose wrapper routing surfaces are currently ready; stubbed surfaces such as `simc` remain visible in `warcraft doctor` and excluded-provider metadata instead of appearing as active wrapper candidates
- the flattened `warcraft search` result list is globally sorted by a tunable wrapper ranking layer that combines provider score, query intent, provider family, and result kind
- flattened wrapper results include `wrapper_ranking` so agents can inspect why a provider/result surfaced first
- `warcraft resolve` uses the same wrapper ranking layer on top of provider confidence instead of trusting provider registration order
- use `--compact` on `warcraft search` or `warcraft resolve` when you want the wrapper decision without the full per-provider payloads
- use `--ranking-debug` when you want compact ranking summaries for the top wrapper candidates
- use `--expansion-debug` when you want a compact per-provider expansion eligibility snapshot
- wrapper ranking policy can be overridden with `~/.config/warcraft/wrapper_ranking.json`
- the wrapper may add synthetic search candidates when a provider has a strong direct command but no native search surface for that query family, such as `wowprogress leaderboard pve ...`
- wrapper `resolve` does not treat those synthetic direct routes as verified resolutions
- wrapper expansion filtering is conservative:
  - `wowhead` is currently the only profiled expansion-aware provider
  - `method`, `icy-veins`, `raiderio`, `wowprogress`, and `warcraftlogs` are currently treated as retail-only when wrapper expansion filtering is active
  - `warcraft-wiki` and `simc` are currently excluded from wrapper expansion-filtered `search` and `resolve`
- wrapper `search`, `resolve`, and `doctor` report included and excluded providers when expansion filtering is active
- wrapper `doctor` also reports wrapper-surface readiness plus provider auth/install metadata, so agents can distinguish a registered provider from a wrapper-ready routing surface
- `--expansion-debug` exposes the full provider eligibility snapshot even in compact mode
- direct passthrough commands reject unsupported provider/expansion combinations instead of silently ignoring the expansion request
- wrapper `doctor` preserves provider registration status, so partial providers stay marked `partial` even when their local doctor command succeeds

## Agent Workflow Direction

- These CLIs are designed as agent-facing building blocks for broad World of Warcraft questions, not just one-off direct lookups.
- Broad requests like "tell me about this class, quest, item, zone, or spec" should route cleanly to the right provider without hiding source provenance.
- Cross-provider requests should stay composable:
  - compare guide providers against each other
  - compare guide-derived recommendations against local `simc` APL behavior for an exact build
  - connect reference, ranking, profile, log, and simulation surfaces without hand-normalizing every identifier
- Deep log-analysis requests should stay scope-safe:
  - prefer typed `warcraftlogs` report and encounter commands over manual event stitching
  - preserve exact fight, player, target, ability, and window provenance in the payload
  - treat sampled analytics as sampled analytics, not global truth
- Not every high-value workflow is a one-command surface yet.
- The current implementation direction is to add the smallest trustworthy primitives needed for agents to compute those workflows safely:
  - shared cross-provider identity and handoff primitives
  - normalized guide-comparison surfaces
  - deeper scoped Warcraft Logs analytics
  - evidence packets with consistent freshness and citation metadata

`warcraft doctor` reports:
- wrapper health
- effective XDG-style config/data/cache/state roots
- active worktree-runtime isolation details when running from an editable worktree
- registered provider readiness
- provider expansion-support mode and active expansion eligibility when `--expansion` is set

## Wowhead Commands

```bash
wowhead search "defias"
wowhead resolve "fairbreeze favors"
wowhead --expansion wotlk search "thunderfury"
wowhead news
wowhead news "hotfixes" --pages 3 --date-from 2026-03-01
wowhead news "hotfixes" --type live --author Jaydaa --pages 2
wowhead news-post /news/midnight-hotfixes-for-march-13th-marl-decor-cost-reduction-class-bugfixes-and-380785
wowhead blue-tracker
wowhead blue-tracker "class tuning" --pages 2 --date-from 2026-03-01
wowhead blue-tracker "class tuning" --region eu --forum "General Discussion"
wowhead blue-topic /blue-tracker/topic/eu/class-tuning-incoming-18-march-610948
wowhead guides classes
wowhead guides classes "death knight"
wowhead guides classes --author Khazakdk --patch-min 120001 --updated-after 2026-02-01
wowhead guides classes --sort updated --limit 10
wowhead talent-calc druid/balance/DAQBBBBQQRUFURYVBEANVVRUVFVVVQCVQhEUEBUEBhVQ
wowhead talent-calc-packet druid/balance/DAQBBBBQQRUFURYVBEANVVRUVFVVVQCVQhEUEBUEBhVQ
wowhead talent-calc-packet druid/balance/DAQBBBBQQRUFURYVBEANVVRUVFVVVQCVQhEUEBUEBhVQ --out ./tmp/balance-packet.json
wowhead profession-tree alchemy/BCuA
wowhead dressing-room "#fz8zz0zb89c8mM8YB8mN8X18mO8ub8mP8uD"
wowhead profiler 97060220/us/illidan/Roguecane
wowhead guide 3143
wowhead guide-full 3143
wowhead guide-export 3143 --out ./tmp/frost-dk-guide
wowhead guide-export 3143 --out ./tmp/frost-dk-guide --hydrate-linked-entities --hydrate-type spell,item --hydrate-limit 100
wowhead guide-bundle-list
wowhead guide-bundle-list --max-age-hours 72
wowhead guide-bundle-search "frost death knight"
wowhead guide-bundle-query "obliterate"
wowhead guide-bundle-inspect 3143
wowhead guide-bundle-index-rebuild
wowhead cache-inspect
wowhead cache-inspect --show-redis-prefixes
wowhead cache-inspect --summary --hide-zero
wowhead cache-repair --expired-only
wowhead guide-bundle-inspect 3143 --summary
wowhead cache-clear --namespace entity_response --expired-only
wowhead guide-bundle-refresh ./tmp/frost-dk-guide
wowhead guide-bundle-refresh 3143 --root ./wowhead_exports --max-age-hours 6
wowhead guide-query ./tmp/frost-dk-guide "bellamy"
wowhead guide-query 3143 "obliterate" --root ./wowhead_exports
wowhead guide-query ./tmp/frost-dk-guide "welcome" --kind sections --section-title overview
wowhead guide-query 3143 "bellamy" --root ./wowhead_exports --kind linked_entities --linked-source multi
wowhead --pretty search "defias"
wowhead --fields query,count,results search "defias"
wowhead entity item 19019
wowhead entity item 19019 --no-include-comments
wowhead entity item 19019 --include-all-comments
wowhead entity faction 529 --no-include-comments
wowhead entity recipe 2549 --no-include-comments
wowhead entity mount 460 --no-include-comments
wowhead entity battle-pet 39 --no-include-comments
wowhead --compact entity item 19019
wowhead --expansion classic entity item 19019
wowhead --fields entity.name,entity.page_url,tooltip.summary,linked_entities entity quest 86739
wowhead --expansion ptr --normalize-canonical-to-expansion entity-page item 19019
wowhead entity-page item 19019 --max-links 100
wowhead comments item 19019 --limit 30 --sort rating
wowhead compare item:19019 item:19351 --comment-sample 2
wowhead compare --preset gear item:19019 item:19351
wowhead compare --preset quest quest:7786 quest:7787
wowhead linked-graph item 19019 --depth 1 --relation npc,quest
wowhead expansions
```

Wowhead command behavior:
- `search` and `resolve` are still the conservative discovery layer for entity and guide lookups
- `news` scans the Wowhead news timeline and supports:
  - optional topic filtering
  - `--date-from`
  - `--date-to`
  - bounded pagination with `--page` and `--pages`
  - explicit scan metadata so agents can see how much timeline history was searched
- `news-post` fetches one specific Wowhead news article page and returns:
  - normalized page metadata
  - extracted text
  - section chunks when the post body contains markup headings
  - author metadata when Wowhead exposes it
  - embedded related/recent-post buckets when Wowhead exposes them
- `news` also supports stable timeline metadata filters from the listing payload:
  - `--author`
  - `--type`
  - result `facets` so agents can see which authors and type buckets matched the scanned window
- `blue-tracker` does the same for the Wowhead blue tracker and is the right surface for topic-over-time blue post research
- `blue-topic` fetches one specific blue-tracker topic page and returns the normalized topic posts with extracted body text
- `blue-topic` also returns a lightweight topic summary:
  - participant list
  - blue-author list
  - richer per-post metadata like author page, forum-area slug, and post ordering
- `blue-tracker` also supports stable timeline metadata filters from the listing payload:
  - `--author`
  - `--region`
  - `--forum`
  - result `facets` so agents can see which authors, regions, and forums matched the scanned window
- `guides <category>` uses the live guide-category listing surface for categories such as `classes`, `professions`, and `raids`
- `guides <category> <query>` filters within the category listing instead of forcing discovery through generic `search`
- `guides <category>` also supports metadata filters that are more reliable than browser-scanning:
  - `--author`
  - `--updated-after`
  - `--updated-before`
  - `--patch-min`
  - `--patch-max`
  - `--sort relevance|updated|published|rating`
  - result `facets` so agents can quickly see which authors and category-path buckets are in the filtered guide set
- Wowhead entity-type handling is driven by a shared internal registry, so search suggestion types, parser support, resolve filters, and hydrate support stop drifting independently
- `talent-calc` decodes calculator state URLs into:
  - class slug
  - spec slug
  - current build code
  - shared `build_identity` metadata that can be handed off directly into `simc`
  - listed embedded builds when the page exposes them
- `talent-calc-packet` is the narrow exact-build producer for explicit Wowhead refs with build codes:
  - it emits an exact `talent_transport_packet` using the explicit talent-calc state URL
  - it preserves page metadata and listed embedded builds alongside that packet when page fetch succeeds; those fields are best-effort and may be absent on fetch/cache failures
  - add `--out <path>` when you want to save just the exact packet JSON for wrapper handoff or parity checks
  - if packet validation fails, the command stops with `invalid_transport_packet` instead of printing or writing malformed packet JSON
- `profession-tree` decodes profession tree state URLs into:
  - profession slug
  - current loadout code
- `dressing-room` currently acts as a stable state inspector:
  - it normalizes the share hash
  - it returns page metadata plus the exact cited state URL
  - it does not yet decode the appearance payload itself
- `profiler` currently acts as a stable state inspector:
  - it normalizes the raw `list=` reference
  - it extracts list id, region, realm, and character name when present
  - it does not yet decode the underlying profile/list contents

## Method Commands

```bash
method doctor
method search "mistweaver monk"
method resolve "mistweaver monk guide"
method guide "mistweaver-monk"
method guide-full "mistweaver-monk"
method guide-export "mistweaver-monk" --out ./tmp/method-mistweaver
method guide-query ./tmp/method-mistweaver "tea serenity"
```

Method guide behavior:
- `search` and `resolve` work against the Method guide sitemap
- the currently supported Method surface is supported guide/article content under `/guides/<slug>` and `/guides/<slug>/<section>`
- currently validated supported families include class guides, profession guides, delve guides, reputation guides, and article guides
- `guide` returns the requested page summary with navigation, linked-entity preview, explicit embedded `build_references`, and additive `analysis_surfaces` when the page exposes comparison-relevant guide topics
- `guide-full` walks the guide navigation and returns all discovered guide pages
- `guide-export` writes a local guide bundle under `./method_exports/` by default
- `guide-query` searches exported Method bundles across sections, navigation links, linked entities, explicit embedded build references, and additive `analysis_surfaces`
- unsupported Method query families such as `tier list` return a `scope_hint` and no search candidates
- unsupported Method URLs such as premium or account pages return structured `invalid_guide_ref` errors
- unsupported index-style roots such as `tier-list` return structured `unsupported_guide_surface` errors

## Icy Veins Commands

```bash
icy-veins doctor
icy-veins search "mistweaver monk guide"
icy-veins resolve "mistweaver monk guide"
icy-veins guide "mistweaver-monk-pve-healing-guide"
icy-veins guide-full "mistweaver-monk-pve-healing-guide"
icy-veins guide-export "mistweaver-monk-pve-healing-guide" --out ./tmp/icy-mistweaver
icy-veins guide-query ./tmp/icy-mistweaver "vivify"
```

Icy Veins guide behavior:
- `search` and `resolve` work against the Icy Veins WoW sitemap for supported guide families
- broad class and role queries prefer the corresponding class hub or role guide, while specialized queries like easy mode or leveling penalize those broad hubs
- supported families include:
  - class hubs
  - role guides
  - spec guides
  - easy mode pages
  - leveling guides
  - PvP guides
  - spec subpages such as builds, rotation, stat priority, gems, gear, spell summary, resources, Mythic+ tips, macros/addons, and simulations
  - raid guides
  - expansion guides
  - special-event guides such as Remix and Torghast pages
- `guide` returns the requested page summary with family metadata, page TOC, linked-entity preview, explicit embedded `build_references`, and additive `analysis_surfaces` when the page exposes comparison-relevant guide topics
- `guide-full` is family-aware:
  - class hubs and role guides stay on the current page
  - spec-family pages walk the related family navigation only
- unsupported or bad WoW refs fail with a structured `invalid_guide_ref`
- unsupported Icy Veins query families such as `patch notes` or `latest class changes` return a `scope_hint` and no search candidates
- representative real-page fixtures cover supported and intentionally unsupported Icy Veins WoW page shapes
- PvP and stat-priority pages are part of the validated supported family set
- resources, macros/addons, Mythic+ tips, and simulations are also part of the validated supported family set
- leveling, builds/talents, rotation, gems/enchants/consumables, and spell-summary pages are also part of the validated supported family set
- `guide-export` writes a local guide bundle under `./icy-veins_exports/` by default
- `guide-query` searches exported Icy Veins bundles across sections, navigation links, linked entities, explicit embedded build references, and additive `analysis_surfaces`

## Raider.IO Commands

```bash
raiderio doctor
raiderio search "liquid"
raiderio resolve "liquid"
raiderio character us illidan Roguecane
raiderio guild us illidan Liquid
raiderio mythic-plus-runs --region world --dungeon all --page 0
raiderio sample mythic-plus-runs --pages 2 --limit 40
raiderio sample mythic-plus-runs --pages 2 --limit 40 --level-min 25 --contains-spec balance
raiderio sample mythic-plus-players --pages 2 --limit 40 --player-limit 100
raiderio distribution mythic-plus-runs --metric dungeon --pages 2 --limit 40
raiderio distribution mythic-plus-runs --metric spec --pages 2 --limit 40
raiderio distribution mythic-plus-runs --metric class --pages 2 --limit 40 --player-region eu
raiderio distribution mythic-plus-players --metric class --pages 2 --limit 40
raiderio threshold mythic-plus-runs --metric score --value 560 --pages 2 --limit 40
```

Raider.IO phase-1 behavior:
- `doctor` reports cache config and phase-1 capability state
- `search` returns ranked character and guild matches with follow-up commands
- structured queries like `guild us illidan Liquid` or `character us illidan Roguecane` probe the direct profile surfaces before falling back to the weaker site search route
- `resolve` picks a next command conservatively and falls back to `search` when the match set is ambiguous
- `character` returns a compact profile summary with guild, Mythic+, and raid progression context
- `guild` returns a compact guild profile with raid progression, raid rankings, and roster preview
- `mythic-plus-runs` returns ranked Mythic+ run summaries from the documented API endpoint
- `sample mythic-plus-runs` is the first sample-backed analytics primitive and returns:
  - normalized run snapshots
  - sample summary
  - freshness
  - leaderboard provenance
  - optional post-sample filtering for level, score, roster role, class, spec, and player region
- `sample mythic-plus-players` derives normalized player snapshots from the sampled run roster set and returns:
  - unique sampled participants
  - appearance counts
  - top sampled run level
  - class/spec/role tags
  - dungeon coverage
  - explicit player truncation metadata when `--player-limit` cuts the deduped participant set
- `distribution mythic-plus-runs` derives distributions from the sampled run set and currently supports:
  - `mythic_level`
  - `dungeon`
  - `role`
  - `player_region`
  - `class`
  - `spec`
  - `composition`
  - `class_composition`
- `distribution mythic-plus-players` derives player-level distributions from deduped sampled participants and currently supports:
  - `appearance_count`
  - `top_mythic_level`
  - `class`
  - `spec`
  - `role`
  - `player_region`
- `threshold mythic-plus-runs` is the first threshold-style primitive and estimates:
  - sampled Mythic+ levels near a target run score
  - sampled run scores near a target Mythic+ level
- filtered analytics preserve the original sampled run count and the excluded run count, so agents can see how thin a narrowed slice became
- player-snapshot analytics also preserve source-player counts and truncation state, so partial participant views are explicit
- threshold outputs are intentionally explicit that they are derived from sampled leaderboard runs, not direct player-rating guarantees
- use the sample/distribution commands when agents need trustworthy building blocks for later reasoning, instead of treating Raider.IO as if it already answers higher-level analytics questions directly

## Warcraft Wiki Commands

```bash
warcraft-wiki doctor
warcraft-wiki search "world of warcraft api"
warcraft-wiki resolve "world of warcraft api"
warcraft-wiki article "World of Warcraft API"
warcraft-wiki article-full "World of Warcraft API"
warcraft-wiki api "CreateFrame"
warcraft-wiki api-full "XML schema"
warcraft-wiki event "OnKeyDown"
warcraft-wiki event-full "Events"
warcraft-wiki article-export "World of Warcraft API" --out ./tmp/wiki-api
warcraft-wiki article-query ./tmp/wiki-api "framexml"
```

Warcraft Wiki behavior:
- `search` and `resolve` use the MediaWiki search API
- `search` and `resolve` apply family-aware ranking for programming pages like `API_CreateFrame`, `UIHANDLER_OnKeyDown`, and framework/system reference pages like `World of Warcraft API`, `Expansion`, and `Renown`
- `search` and `resolve` also clean out low-value leading family hint terms like `faction`, `lore`, `guide`, `zone`, `profession`, `class`, and `expansion` when there is a stronger article target underneath, and report the cleanup in `excluded_terms`
- `api` and `api-full` are the preferred typed programming surfaces for API functions, framework pages, XML schema pages, console-variable reference pages, and API-change pages
- `event` and `event-full` are the preferred typed programming surfaces for UI handler pages and event/framework pages
- `article` returns a compact article summary with section navigation, linked wiki-article preview, and extracted `reference` metadata
- `article-full` returns the parsed article payload used for local export, including top-level and per-page `reference` metadata
- `article-export` writes a local article bundle under `./warcraft-wiki_exports/` by default
- `article-query` searches exported wiki bundles across sections, navigation links, and linked entities
- programming pages strip low-value wiki chrome more aggressively and filter edit-action links from linked-entity output
- `reference` metadata is useful beyond API pages: programming howtos, API-change pages, class pages, profession pages, faction pages, zone pages, expansion pages, systems pages, guide pages, and lore pages all expose at least a family-aware summary, and some pages also expose `patch_changes`, `see_also`, and `references`
- redirect-backed article lookups follow MediaWiki redirects, so short refs like `Legion` resolve to canonical pages like `World of Warcraft: Legion`

## WowProgress Commands

```bash
wowprogress doctor
wowprogress search "guild us illidan Liquid"
wowprogress resolve "character us illidan Imonthegcd"
wowprogress guild-history us "Mal'Ganis" gn
wowprogress guild-ranks us "Mal'Ganis" gn
wowprogress guild us illidan Liquid
wowprogress character us illidan Imonthegcd
wowprogress leaderboard pve us --limit 10
wowprogress leaderboard pve us --realm illidan --limit 10
wowprogress sample pve-leaderboard --region us --limit 25
wowprogress sample pve-guild-profiles --region us --limit 10
wowprogress sample pve-guild-profiles --region us --limit 10 --faction horde --world-rank-max 25
wowprogress distribution pve-leaderboard --region us --metric progress --limit 25
wowprogress distribution pve-guild-profiles --region us --metric faction --limit 10
wowprogress distribution pve-guild-profiles --region us --metric item_level_average --faction horde
wowprogress threshold pve-leaderboard --region us --metric rank --value 25 --limit 50
wowprogress threshold pve-guild-profiles --region us --metric world_rank --value 25 --limit 10
```

WowProgress phase-1 behavior:
- `doctor` reports cache config and the browser-fingerprint HTTP transport used for live fetches
- `search` expects structured queries like `us illidan Liquid`, `guild us illidan Liquid`, or `character us illidan Imonthegcd`
- direct guild and character commands normalize common region and realm variants like `na` and `Mal'Ganis`
- `search` normalizes some realm forms like `area 52` -> `area-52` and returns `normalized_candidates` so the cleaned structured targets stay visible
- `search` can exclude unsupported trailing terms like `recruit` and reports them in `excluded_terms` with a `normalization_hint`
- `resolve` uses the same structured query shape and only returns a next command when the route probe is unambiguous
- direct route resolution handles canonical WowProgress realm formatting, so queries like `guild us area-52 xD` still resolve correctly even when the site returns `US-Area 52`
- `guild` returns a compact guild profile with progression, item-level rank context, and encounter history
- `guild-history` walks the guild's historical tier pages and returns a per-tier progression timeline with final rank snapshots
- `guild-ranks` returns the condensed per-tier final-rank view for questions like "final ranks across tiers"
- `character` returns a compact character profile with item-level, SimDPS, and PvE raid-history context
- `leaderboard pve` returns the current PvE progression leaderboard for a region, optionally narrowed to a realm
- `sample pve-leaderboard` returns a top-slice leaderboard sample with explicit sampling metadata for the requested row cap and returned entry count
- `sample pve-guild-profiles` enriches the sampled leaderboard slice with direct guild-page data and reports:
  - source leaderboard entry count
  - returned guild profile count
  - skipped rows without profile URLs
- guild-profile analytics support explicit post-sample filters for:
  - faction
  - difficulty
  - world rank range
  - item-level range
  - encounter name
- filtered guild-profile analytics preserve source and excluded profile counts so narrower slices stay explicit
- `distribution` and `threshold` stay sample-backed and caveated rather than pretending to answer higher-level questions directly
- WowProgress search is intentionally structured instead of broad free text because the site-native search surface is heavily constrained and less reliable than direct route resolution

## Warcraft Logs Commands

```bash
warcraftlogs doctor
warcraftlogs auth status
warcraftlogs auth client
warcraftlogs auth token
warcraftlogs auth whoami
warcraftlogs auth login --redirect-uri http://127.0.0.1:8787/callback
warcraftlogs auth pkce-login --redirect-uri http://127.0.0.1:8787/callback
warcraftlogs auth logout
warcraftlogs rate-limit
warcraftlogs regions
warcraftlogs expansions
warcraftlogs server us illidan
warcraftlogs zones
warcraftlogs zones --expansion-id 12
warcraftlogs zone 38
warcraftlogs encounter 3012
warcraftlogs guild us illidan Liquid
warcraftlogs guild us illidan Liquid --zone-id 38
warcraftlogs guild-members us illidan Liquid --limit 5
warcraftlogs guild-attendance us illidan Liquid --limit 2
warcraftlogs guild-rankings us illidan Liquid --zone-id 38 --size 20 --difficulty 5
warcraftlogs guild-reports us illidan Liquid --limit 10
warcraftlogs character us illidan Roguecane
warcraftlogs character-rankings us illidan Roguecane --zone-id 38 --difficulty 5 --metric dps --size 20
warcraftlogs encounter-rankings --zone-id 46 --boss-id 3180 --difficulty 5 --class-name Druid --spec-name Balance --metric dps --top 10
warcraftlogs reports --guild-region us --guild-realm illidan --guild-name Liquid --limit 10
warcraftlogs report abcdefgh
warcraftlogs report-fights abcdefgh --difficulty 5
warcraftlogs report-player-details abcdefgh --fight-id 47
warcraftlogs report-player-talents abcdefgh --fight-id 47 --actor-id 1234
warcraftlogs report-player-talents abcdefgh --fight-id 47 --actor-id 1234 --out ./tmp/gubkfc-packet.json
warcraftlogs report-master-data abcdefgh --actor-type Player
warcraftlogs report-events abcdefgh --fight-id 47 --limit 100
warcraftlogs report-table abcdefgh --data-type damage-done --fight-id 47
warcraftlogs report-graph abcdefgh --data-type damage-done --fight-id 47
warcraftlogs report-rankings abcdefgh --fight-id 47 --player-metric dps --timeframe historical --compare rankings
warcraftlogs graphql --query 'query Report($code: String!) { reportData { report(code: $code) { code title } } }' --report-code abcdefgh
warcraftlogs graphql --query @./query.graphql --variables-json '{"code":"abcdefgh"}' --operation-name Report
warcraftlogs graphql --introspect
warcraftlogs report-encounter 'https://www.warcraftlogs.com/reports/abcdefgh#fight=47'
warcraftlogs report-encounter-players 'https://www.warcraftlogs.com/reports/abcdefgh#fight=47'
warcraftlogs report-encounter-casts 'https://www.warcraftlogs.com/reports/abcdefgh#fight=47' --preview-limit 20
warcraftlogs report-encounter-buffs 'https://www.warcraftlogs.com/reports/abcdefgh#fight=47' --view-by source
warcraftlogs report-encounter-aura-summary 'https://www.warcraftlogs.com/reports/abcdefgh#fight=47' --ability-id 20473 --window-start-ms 30000 --window-end-ms 90000
warcraftlogs report-encounter-aura-compare 'https://www.warcraftlogs.com/reports/abcdefgh#fight=47' --ability-id 20473 --left-window-start-ms 30000 --left-window-end-ms 90000 --right-window-start-ms 90000 --right-window-end-ms 150000
warcraftlogs report-encounter-damage-source-summary 'https://www.warcraftlogs.com/reports/abcdefgh#fight=47' --window-start-ms 30000 --window-end-ms 90000
warcraftlogs report-encounter-damage-target-summary 'https://www.warcraftlogs.com/reports/abcdefgh#fight=47' --window-start-ms 30000 --window-end-ms 90000
warcraftlogs report-encounter-damage-breakdown 'https://www.warcraftlogs.com/reports/abcdefgh#fight=47' --window-start-ms 30000 --window-end-ms 90000
warcraftlogs boss-kills --zone-id 38 --boss-id 3012 --difficulty 5 --top 10
warcraftlogs top-kills --zone-id 38 --boss-name 'Dimensius' --difficulty 5 --top 5
warcraftlogs kill-time-distribution --zone-id 38 --boss-id 3012 --difficulty 5 --bucket-seconds 30
warcraftlogs boss-spec-usage --zone-id 38 --boss-id 3012 --difficulty 5 --top 10
warcraftlogs comp-samples --zone-id 38 --boss-id 3012 --difficulty 5 --top 5
warcraftlogs ability-usage-summary --zone-id 38 --boss-id 3012 --difficulty 5 --ability-id 20473 --preview-limit 5
```

Current Warcraft Logs provider behavior:
- `warcraftlogs` currently targets the retail/main site profile only
- public OAuth client credentials are the default auth mode
- manual user-auth groundwork is available for:
  - authorization code
  - PKCE
  - saved user-token verification via `warcraftlogs auth whoami`
- credentials are loaded in this priority order:
  - repo-local `.env.local`
  - XDG config: `~/.config/warcraft/providers/warcraftlogs.env`
  - process environment
- runtime auth state is stored separately under:
  - XDG state: `~/.local/state/warcraft/providers/warcraftlogs.json`
- supported auth variables:
  - `WARCRAFTLOGS_CLIENT_ID`
  - `WARCRAFTLOGS_CLIENT_SECRET`
- example XDG provider config:
```bash
mkdir -p ~/.config/warcraft/providers
cat > ~/.config/warcraft/providers/warcraftlogs.env <<'EOF'
WARCRAFTLOGS_CLIENT_ID=...
WARCRAFTLOGS_CLIENT_SECRET=...
EOF
```
- `doctor` reports auth status plus the active site profile
- `doctor` and `auth status` now also distinguish:
  - `public_api_access`: whether public report/world/guild commands are runnable right now
  - `user_api_access`: whether saved user-auth commands are runnable right now
  - runtime mode: public commands use `client_credentials`; saved user auth is reported separately
- `doctor` and `auth status` now run live Warcraft Logs probes by default:
  - public auth is validated with a `rate-limit` probe when client credentials are configured
  - saved user auth is validated with `auth whoami`-equivalent `current_user` access when a saved user token exists
  - pass `--no-live` to either command when you want local/runtime-only diagnostics without remote validation
- `auth status` reports credential source, runtime auth-state presence, and which grant types are currently implemented
- `auth client` reports the configured client metadata and endpoint URLs without exposing the secret
- `auth token` reports persisted token metadata without printing raw tokens
- `auth whoami` uses the saved user token against the private `/api/v2/user` endpoint and is the clearest direct verification that saved user auth is actually usable
- in `doctor`, `capabilities.user_auth` reports `ready_manual_exchange` when `auth login` / `auth pkce-login` can bootstrap user auth but no saved user token exists yet
- if client credentials are not available, public data commands fail with `missing_public_auth` instead of a generic `missing_auth`
- `auth login` and `auth pkce-login` now validate client credentials before writing pending auth state, so a failed login bootstrap does not clobber the saved auth-state file
- `auth login --redirect-uri ...` supports a manual two-step authorization-code flow:
  - run it once to get the authorize URL
  - complete the browser consent flow
  - run it again with `--code` and `--state` from the callback URL
- add `--scope view-user-profile` when you want a token that can access current-user profile fields
- `auth pkce-login --redirect-uri ...` does the same for PKCE and stores the pending verifier in the XDG auth-state file
- `auth logout` clears the local persisted auth state
- `rate-limit` exposes the official queryable API rate-limit state
- `regions`, `expansions`, `server`, `zones`, `zone`, and `encounter` are the first typed world-metadata slice
- `guild` returns official guild identity, server/faction details, guild tags, and current zone progress ranks when available
- `guild-members` returns the Warcraft Logs guild roster plus pagination metadata for games where roster verification is supported
- `guild-attendance` returns raid-night attendance history plus per-player presence markers
- `guild-rankings` exposes the official `zoneRanking` progress/speed payloads directly
- `guild-reports` is the convenient guild-scoped report listing surface on top of the official paginated report query
- `character` currently stays on reliable typed fields:
  - faction
  - guild rank
  - server
  - guild memberships
- `character-rankings` is available, but Warcraft Logs may return per-character permission errors or server-side errors for some characters; the CLI surfaces provider permission errors explicitly in the payload when they are available
- `encounter-rankings` is the official encounter leaderboard surface for boss/class/spec ranking queries
  - returned row `rank` values are derived from page order when Warcraft Logs omits per-row rank fields
  - pagination counts in the normalized payload are page-scoped; the provider does not currently expose a stable global total for this surface
- `reports`, `report`, and `report-fights` are the first typed report-inspection slice
- `report-fights` stays on the stable broad fight-list contract for now; deeper fight-filter and phase workflows are still deferred
- `report-player-details` exposes role buckets and participant summaries for a report or fight slice
- `report-player-talents` is the first scoped talent-transport producer:
  - one report
  - one fight
  - one actor id
  - for normal multi-fight reports, supply encounter scope via `--fight-id` or a scoped report URL
  - it only emits a packet when every selected `combatant_info.talentTree` row is fully formed
  - it preserves normalized raw talent-tree rows as `entry/node_id/rank` evidence from `combatant_info.talentTree`
  - when local SimulationCraft trait data can resolve every row and the reconstructed build round-trips, it also emits validated `simc_split_talents`
  - otherwise it stays `raw_only` and reports why validation could not be proven
  - add `--out <path>` when you want the command to write just the packet JSON for a later `simc` or wrapper handoff
  - malformed or incomplete talent-tree rows fail with `missing_talent_tree` instead of emitting a partial packet
  - if packet validation fails, the command stops with `invalid_transport_packet` instead of printing or writing malformed packet JSON
- `report-master-data` exposes report actor and ability catalogs, which is often the most useful companion surface for deeper report analysis
- `report-table` and `report-graph` accept friendly enum-like filters such as `damage-done` and normalize them to the official GraphQL enum values
- `report-rankings` exposes the official report rankings JSON with typed query metadata
- `graphql` runs raw official GraphQL through the same auth, endpoint routing, cache opt-in, and partial-error envelope as typed commands:
  - `--query` accepts literal text, `@path`, or `-` for stdin
  - `--variables-json` accepts a JSON object, while repeatable `--var key=value` JSON-coerces individual values and wins on conflicts
  - scoping helpers such as `--report-code`, `--fight-id`, `--encounter-id`, `--zone-id`, `--source-id`, `--target-id`, and `--ability-id` inject only variables declared by the operation
  - `--endpoint auto` uses saved user auth when available, `--endpoint client` forces public client credentials, and `--endpoint user` forces saved user auth
  - `--cache-ttl` defaults to `0` and is opt-in for client-endpoint queries; user-endpoint raw queries are not cached
  - `--introspect` returns the provider schema introspection payload
  - full scoping rules live in [docs/warcraftlogs/SCOPING.md](warcraftlogs/SCOPING.md)
- `report-encounter`, `report-encounter-players`, `report-encounter-casts`, `report-encounter-buffs`, and `report-encounter-damage-breakdown` are the first deep encounter-analysis slice:
  - they accept either a report code plus `--fight-id`
  - or a Warcraft Logs report URL with a numeric `#fight=...` fragment
  - they return one explicitly selected fight instead of making the agent reconstruct scope manually
  - the cast/buff/damage variants support encounter-relative timeline windows with:
    - `--window-start-ms`
    - `--window-end-ms`
  - these commands preserve the resolved absolute report timestamps in the payload so the agent does not have to calculate them
  - `report-encounter-casts` includes additive `by_target` and `by_source_target` summaries so agents can compare spell usage across encounter targets without dropping to raw events
- `report-encounter-aura-summary` is the narrower aura lane:
  - it requires one explicit `--ability-id`
  - it stays on one selected encounter plus optional encounter-relative windowing
  - it returns typed source rows with preserved reported buff-table fields instead of asking agents to infer aura windows from raw events
- `report-encounter-aura-compare` is the strict comparison layer on top of that:
  - same report
  - same fight
  - same explicit aura
  - two fully explicit encounter-relative windows
  - typed per-source deltas instead of inferred pull-to-pull comparisons
- `report-encounter-damage-source-summary` is the fixed-source damage lane:
  - same explicit encounter scope as the other `report-encounter*` commands
  - fixed `view_by=Source`
  - typed source rows plus preserved raw reported fields
- `report-encounter-damage-target-summary` is the parallel target lane:
  - same explicit encounter scope
  - fixed `view_by=Target`
  - typed target rows plus preserved raw reported fields
- `boss-kills`, `top-kills`, `kill-time-distribution`, `boss-spec-usage`, `comp-samples`, and `ability-usage-summary` are the current sampled cross-report analytics slice:
  - they sample public finished reports for one zone
  - they rank within the sampled cohort, not all possible Warcraft Logs data
  - they expose sample, exclusion, truncation, freshness, and citation metadata so the agent does not have to pretend the sample is global
  - on these sampled commands, `--spec-name` filters sampled kills by matching participant specs before aggregation; it does not turn the result into a spec leaderboard query
  - `boss-spec-usage` summarizes spec presence inside the sampled finished-kill cohort after boss/difficulty/spec/time filters have already been applied
  - `comp-samples` returns sampled kill rosters plus additive class-presence and exact class-signature summaries for the sampled cohort
  - `ability-usage-summary` summarizes one explicit `--ability-id` across that same sampled finished-kill cohort and reports cast counts per kill instead of inferring broader gameplay conclusions
- `report-events` is available now, but it intentionally requires a narrowed slice:
  - `--fight-id`
  - `--encounter-id`
  - `--start-time`
  - `--end-time`
- `report-events` can still return `events: null` for some valid report slices; treat it as a typed paginator surface, not as a guarantee of non-empty event data for every query
- `report-rankings` can legitimately return zero rows for a valid report slice, so treat it as a report-ranking surface, not as a guarantee that public rankings exist for every fight
- Warcraft Logs documents that guild roster verification is game-dependent, so `guild-members` should be treated as a retail-capable roster surface, not a universal guarantee across every future site profile
- `guild-attendance` is available as an official schema surface, but live public queries can still hit provider-side internal errors; treat it as useful when it works, not as a guaranteed stable contract yet
- cross-report analytics skip unfinished live reports and currently treat only finished reports as stable sampled inputs
- wrapper integration is intentionally deferred for now

## SimulationCraft Commands

```bash
simc doctor
simc repo
simc repo --set-root <simc-root>
simc checkout
simc version
simc verify-clean
simc inspect
simc inspect <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc
simc spec-files mistweaver
simc identify-build --build-text 'CgcBG5bbocFKcv+yIq8fPd6ORBA2MmZmxMzMGzMAAAAAAAegxsNYGAAAAAAAAmxMMmZmZmZmZGzsYGjFtsxMzMzWbzMzAYYAIwMGMmB'
simc identify-build --build-text 'https://www.wowhead.com/talent-calc/demon-hunter/devourer/CgcBG5bbocFKcv+yIq8fPd6ORBA2MmZmxMzMGzMAAAAAAAegxsNYGAAAAAAAAmxMMmZmZmZmZGzsYGjFtsxMzMzWbzMzAYYAIwMGMmB'
simc identify-build --build-packet ./build-packet.json
simc validate-talent-transport --build-packet ./build-packet.json
simc validate-talent-transport --actor-class druid --spec balance --talent-row 103324:82244:1 --talent-row 109839:88206:1 --talent-row 117176:94585:1
simc describe-build --build-text 'CgcBG5bbocFKcv+yIq8fPd6ORBA2MmZmxMzMGzMAAAAAAAegxsNYGAAAAAAAAmxMMmZmZmZmZGzsYGjFtsxMzMzWbzMzAYYAIwMGMmB'
simc describe-build --build-packet ./build-packet.json --apl-path <simc-root>/ActionPriorityLists/default/druid_balance.simc
simc decode-build --apl-path <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc --talents ABC123
simc decode-build --build-text 'CgcBG5bbocFKcv+yIq8fPd6ORBA2MmZmxMzMGzMAAAAAAAegxsNYGAAAAAAAAmxMMmZmZmZmZGzsYGjFtsxMzMzWbzMzAYYAIwMGMmB'
simc decode-build --build-text $'demonhunter="probe"\nspec=devourer\ntalents=CgcBG5bbocFKcv+yIq8fPd6ORBA2MmZmxMzMGzMAAAAAAAegxsNYGAAAAAAAAmxMMmZmZmZmZGzsYGjFtsxMzMzWbzMzAYYAIwMGMmB'
simc decode-build --build-packet ./build-packet.json
simc sim ./profile.simc
cat ./profile.simc | simc sim -
simc sim ./profile.simc --preset high-accuracy
simc build-harness --actor-class warlock --spec demonology --talents ABC123 --line hero_talents=2 --line fight_style=Patchwerk
simc validate-apl ./demonology_harness.simc ./warlock_demonology.simc --label base
simc compare-apls ./demonology_harness.simc --base-apl ./warlock_demonology.simc --variant wowhead=./wowhead_variant.simc --variant icyveins=./icy_variant.simc --report-out ./compare.json
simc variant-report ./compare.json
simc apl-lists <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc
simc apl-graph <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc
simc apl-talents <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc
simc find-action rising_sun_kick --class monk
simc trace-action <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc rising_sun_kick --class monk
simc apl-prune <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc --targets 1
simc apl-branch-trace <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc --targets 1
simc apl-intent <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc --targets 1
simc apl-intent-explain <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc --targets 1
simc priority <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc --targets 5 --talents ABC123
simc inactive-actions <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc --targets 5 --talents ABC123
simc opener <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc --targets 5 --talents ABC123
simc apl-branch-compare <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc --left-targets 3 --right-targets 1
simc analysis-packet <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc --targets 1
simc first-cast <simc-root>/profiles/MID1/MID1_Monk_Windwalker.simc tiger_palm --seeds 1 --max-time 20
simc log-actions /tmp/simc-cli-example/seed_1.log tiger_palm rising_sun_kick
simc compare-builds --base 'TALENT_STRING_A' --other 'TALENT_STRING_B' --actor-class druid --spec balance
simc compare-builds --base 'TALENT_STRING_A' --other 'TALENT_STRING_B' --other 'TALENT_STRING_C' --tree class
simc modify-build --talents 'TALENT_STRING' --swap-class-tree-from 'OTHER_TALENT_STRING' --actor-class druid --spec balance
simc modify-build --talents 'TALENT_STRING' --add 'forestwalk:2' --remove 'innervate' --actor-class druid --spec balance
simc run ./profile.simc --arg iterations=1 --arg desired_targets=1
simc sync
simc build
```

SimulationCraft behavior:
- `doctor` reports repo path, git status, binary presence, phase capability state, and repo-resolution source (`cli_override`, `env`, `config`, `managed`, or `unset`)
- `repo` shows the active repo-resolution path and can persist or clear an explicit repo root
- `checkout` performs an optional CLI-managed checkout or update under the XDG data root
- `version` probes the local `simc` binary and extracts the printed SimulationCraft version line
- `verify-clean` reports upstream git cleanliness and the local binary state, with optional binary hashing
- `inspect` returns either repo state or file-level inspection data, including inferred actor/spec and extracted build lines for `.simc` files
- `spec-files` searches the local checkout across APL files and, when queried, matching class modules and spell dumps
- `identify-build` is the safest first step when the user pastes a build string or exact Wowhead talent-calc URL with build code; it reports `source_kind`, resolved class/spec, confidence, and any probe candidates before deeper analysis
- `identify-build`, `decode-build`, and `describe-build` also accept `--build-packet <path>` for talent transport packet JSON:
  - exact forms such as embedded Wowhead URLs or WoW export strings are preferred first
  - standalone exact `wow_talent_export` packets do not treat packet-supplied class/spec as trusted identity on their own; `simc` still falls back to safer inference or bounded probing when needed
  - validated reconstructed forms such as `simc_split_talents` are used when no exact form exists, and they stay bound to the class/spec they were validated under
  - raw-only packets must be upgraded through `simc validate-talent-transport` before they can drive build analysis
  - the returned `build_spec.transport_packet` block records only the packet `path`, chosen `transport_form`, and `transport_status`; packet provenance continues to live in `source_notes`
  - malformed packet files now fail consistently with `invalid_build_packet`
- `validate-talent-transport` is the reusable packet-validation primitive:
  - use `--build-packet <path>` when another integration already emitted a raw scoped packet
  - or pass repeated `--talent-row entry_id:node_id:rank` with explicit `--actor-class` and `--spec`
  - it keeps the raw rows visible, resolves entries through local SimC trait data, and only emits `simc_split_talents` when the reconstructed build round-trips cleanly
  - malformed packet files also fail with `invalid_build_packet`, matching the other exact-build packet entrypoints
- `--talents` accepts the same common consumer inputs as `--build-text` for exact-build commands:
  - bare WoW talent export strings
  - Wowhead talent-calc URLs with build codes
  - SimC `talents=...` lines
- `describe-build` is the safest first step when the user says “tell me about this build”; it combines:
  - build identity
  - selected vs skipped talents
  - single-target and multi-target focus lists
  - notable inactive talent-gated branches
  - ST vs AoE active-action deltas
  - `focus_path` and `focus_resolution` metadata when the CLI can follow dispatcher lists into a leaf priority list
- `decode-build` uses the local `simc` binary to decode talent strings into enabled talents and tree-grouped talent rows
- `decode-build` accepts:
  - a bare WoW talent export string
  - a Wowhead talent-calc URL with a build code
  - SimC-native build/profile text
  - a talent transport packet JSON path via `--build-packet`
  and reports both the detected `source_kind` and the normalized generated SimC profile it used for decoding
- `decode-build` only treats talents with positive ranks as enabled; `0/1` rows like a skipped capstone stay in the `skipped` side of `describe-build`
- if class/spec are not supplied explicitly, `decode-build`, `build-harness`, and the exact-build APL commands try to identify them automatically:
  - direct actor/spec lines or APL path inference win first
  - Wowhead talent-calc URLs with build codes contribute class/spec directly from the URL path
  - bare WoW talent exports fall back to a bounded local SimC probe across supported specs
- `sim` is the preferred consumer run path:
  - supports profile files, `stdin`, or `--profile-text`
  - uses explicit fixed presets instead of implicit adaptive settings
  - always returns run settings, runtime timing, and core metrics
- `build-harness` writes a reusable local profile harness so APL comparisons can swap only the action list
- `validate-apl` builds a temporary harness+APL profile and runs a cheap one-iteration validation pass
- `compare-apls` compares one base APL plus labeled variants on the same harness and returns DPS plus action-throughput deltas from SimC JSON output
- `variant-report` summarizes a saved `compare-apls` report into ranking and delta rows
- use `1000` iterations for most work
- use `5000+` iterations only when the user explicitly wants higher accuracy
- `sim --preset quick` is the default `1000`-iteration path
- `sim --preset high-accuracy` is the default `5000`-iteration path
- do not recommend a fixed thread count without checking the current machine; omit `threads` or inspect the environment first
- `apl-lists` returns parsed action lists and their entries from a local `.simc` file
- `apl-graph` emits a Mermaid action-list call graph from a local `.simc` file
- `apl-talents` returns talent gate references and a compact action frequency summary for a local `.simc` file
- `find-action` searches local APLs, class modules, and spell dumps for an action token
- `trace-action` combines local APL hits with broader repo search hits for one action token
- `apl-prune` classifies APL lines conservatively as `eligible`, `dead`, or `unknown` using decoded talents plus target count
- `apl-branch-trace` traces likely `run_action_list` and `call_action_list` flow through one APL
- `apl-intent` summarizes the early likely priorities in the selected focus list after branch evaluation
- `apl-intent-explain` groups the early likely priorities into setup, helper, burst, and remaining priority buckets
- `priority` is the preferred exact-build static priority view when the caller provides a talent string; it returns active priorities for the resolved focus list and separately surfaces inactive talent-gated branches
- `inactive-actions` is the direct audit command for confirming which actions are excluded for the current build, with `--talent-only` on by default
- `opener` returns a static exact-build early-action preview and explicitly warns when runtime-sensitive conditions still matter
- `apl-branch-compare` compares branch and focus-list changes between two target/build contexts
- `analysis-packet` emits an agent-facing summary with branch certainty, intent lines, explained intent, escalation reasons, recommended next steps, and optional first-cast timing samples
- if the user supplies a talent string, assume inactive talent branches should be removed from summaries; use `priority` or `inactive-actions` before making rotation claims from a shared APL
- if the user wants to compare guide-shaped rotations or draft APLs, use `build-harness`, `validate-apl`, and `compare-apls` instead of editing upstream files
- `first-cast` runs short one-iteration sims and records the first observed execution time for a named action across one or more seeds
- `log-actions` inspects an existing SimC combat log and extracts the first scheduled and performed timestamps for named actions
- `compare-builds` diffs talent selections between a base build and one or more other builds, grouped by tree (class, spec, hero); use `--tree` to limit the diff to specific trees
- `modify-build` produces a new WoW talent export string from an existing build after applying modifications:
  - `--swap-class-tree-from`, `--swap-spec-tree-from`, `--swap-hero-tree-from` replace an entire tree's talents from another build
  - `--add name:rank` or `--add entry_id:rank` adds or sets individual talents (SimC resolves both names and entry IDs)
  - `--remove name` or `--remove entry_id` removes individual talents from the base build
  - tree swaps and individual overrides can be combined in one invocation
  - output includes the encoded export string, a Wowhead talent-calc URL, and a per-tree diff from the base build
  - encoding uses SimC's own `generate_traits_hash()` via `save=`, not reverse-engineered client-side encoding
- `run` executes the local `simc` binary against a profile and returns bounded stdout/stderr previews
- `sync` and `build` are conservative local repo helpers; `sync` skips dirty worktrees unless `--allow-dirty` is set
- `search` and `resolve` exist for wrapper-contract stability, but return structured `coming_soon` payloads until SimC discovery is implemented
- repo resolution supports CLI overrides, `SIMC_REPO_ROOT`, persisted config, a CLI-managed checkout, and a clean-install `unset` state that points at the managed path before checkout

## Output Conventions

- Default output is compact JSON for machine consumption.
- Use `--pretty` for human-readable JSON.
- Successful responses omit `ok`.
- Structured failures return `ok: false` with an `error` object.
- Use `--fields` to project only selected dot-paths from the JSON payload.
- Use `--fields-strict` to fail when a requested `--fields` path is missing.
- Use `--compact` to truncate long string fields such as tooltip HTML blobs.
- Use `--compact-max-chars` to tune truncation length (default `280`).
- Use `--profile agent|human|debug` for output presets (`human` = pretty JSON, `debug` = pretty JSON plus a `diagnostics` block when present).
- Use `--citation-pack` on Wowhead commands to attach a deterministic `citation_pack` with source URLs and per-claim anchors (`entity`, `compare`, and similar payloads).
- Wrapper responses preserve provider provenance instead of flattening everything into a fake universal schema.

## Expansion And Routing

- Use global `--expansion` to target a version profile; default is `retail`.
- Use `--normalize-canonical-to-expansion` if you want canonical entity page URLs forced into the selected expansion path.
- Some entity types use special routing under the hood:
  - `faction` and `pet` use page-metadata tooltip fallbacks
  - `recipe` resolves through spell pages
  - `mount` resolves through underlying item pages
  - `battle-pet` resolves through underlying NPC pages

See [wowhead/EXPANSION_RESEARCH.md](wowhead/EXPANSION_RESEARCH.md) for the routing and `dataEnv` findings behind this behavior.

## Entity Retrieval

- `entity` is the compact main retrieval command.
- `entity-page` is the richer page exploration command.
- `comments` is the comment-focused command.

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
- when the query contains follow-up words like `comments`, `links`, or `full`, the CLI strips those from the upstream Wowhead lookup and exposes the actual request text as `search_query`
- use `resolve` when you want the CLI to choose the best next command conservatively
- `resolve` reuses the same follow-up guidance, but only emits `next_command` when confidence is high
- `resolve --entity-type guide` or similar can safely narrow ambiguous queries when the caller already knows the target class of thing

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

Useful filters:
- `--kind`
- `--section-title`
- `--linked-source href|gatherer|multi`

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

- [ROADMAP.md](ROADMAP.md)
- [wowhead/ACCESS_METHODS.md](wowhead/ACCESS_METHODS.md)
- [wowhead/EXPANSION_RESEARCH.md](wowhead/EXPANSION_RESEARCH.md)

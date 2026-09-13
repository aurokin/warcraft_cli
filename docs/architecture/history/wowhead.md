# Wowhead Provider History

Design record for the first provider CLI. Kept for provenance; the current behavior of `wowhead`
lives in [docs/wowhead/README.md](../../wowhead/README.md).

## Role In The Monorepo

Wowhead was built first and was the most mature surface when the monorepo restructure started. It
became the reference implementation for agent-first CLI output, local bundle workflows, and cache
inspection, and the first consumer of every shared package extracted since.

What moved out of `wowhead_cli` into shared packages:

- output shaping and `--fields` projection, structured error helpers, and CLI scaffolding
  (`warcraft_core.output`, `warcraft_core.cli`, `warcraft_core.envelope`)
- cache backends, TTL and config plumbing, HTTP transport and retry (`warcraft_api`)
- guide/article analysis surfaces (`warcraft_content`)
- the expansion vocabulary — keys, aliases, and per-site mapping (`warcraft_core.expansions`);
  `wowhead_cli.expansion_profiles` now layers only Wowhead facts (path prefix, `data_env`, legacy
  subdomains) on top of it

What stayed Wowhead-specific: HTML parsing rules, page JSON extraction, entity routing quirks,
guide-body extraction, and Wowhead ranking and link normalization.

## Type Registry Drift

An early review found Wowhead type support spread across several registries that were already
drifting: search suggestions mapped type `112` to `companion`, but `companion` was not consistently
supported across entity parsing, hydrate support, search hints, and resolve filters. That was
consolidated into one canonical internal registry (`wowhead_cli.entity_types`) before any further
database families were added.

## Database Pages: Deliberately Deferred

Live Wowhead exposes database-family pages (`/database`, `/items`, `/npcs`, `/quests`, `/spells`,
`/achievements`, `/zones`, `/maps`, `/objects`, `/factions`, `/currencies`, `/skills`,
`/item-sets`, `/followers`, `/titles`) and a generic `wowhead db <family>` command was considered.

Decision: do not implement generic database browsing just because the pages exist. The direct
`entity`, `entity-page`, `comments`, `search`, `resolve`, `guide`, `news`, `blue-tracker`, and
`guides <category>` commands answer most real workflows more reliably. Database pages become worth
the parser complexity only for a concrete bulk browse/filter workflow the current commands cannot
cover. This keeps the CLI biased toward reliable structured retrieval instead of broad but fragile
page-surface coverage.

## Tool Surfaces: The Maintainability Boundary

`talent-calc`, `talent-calc-packet`, and `profession-tree` are real route-state decoders because
the state is in the URL and in embedded JSON.

`dressing-room` and `profiler` deliberately stop at stable route-state inspection. Decoding the
appearance payload or the underlying profile/list contents is not straightforward HTML or embedded
JSON extraction; it is a separate reverse-engineering project. That work should start only with an
explicit product decision and a concrete user workflow that justifies the complexity.

## News And Blue Tracker

`news`, `news-post`, `blue-tracker`, and `blue-topic` were added because generic `search` and
`resolve` were not a reliable substitute for those surfaces, and because agents typically want
topic context over a window of time rather than only the newest post. The listing commands
therefore carry `query`, `date_from`, `date_to`, `limit`, and bounded pagination, and the response
exposes publish timestamps, source URLs, listing query provenance, and truncation state.

Still open: category/filter narrowing when the live page model allows it, and deeper post/topic
enrichment beyond the first detail-fetch slice.

## Search And Resolve Boundary

`search` and `resolve` stay discovery surfaces: candidate search, conservative resolution, and
follow-up command guidance between `entity`, `entity-page`, `comments`, `guide`, and `guide-full`.
They are routing aids, not analytics answer surfaces, and follow-up recommendations must not be
stretched into answer synthesis beyond what the retrieved data shows.

## Remaining Gaps

- database-family browsing and filtering
- deeper tool decoding beyond the first tool-state slice
- guide category coverage beyond the first listing surface
- deeper timeline filtering and enrichment beyond the first listing/detail summaries

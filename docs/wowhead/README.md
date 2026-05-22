# Wowhead CLI

Companion docs in this folder:
- [ACCESS_METHODS.md](ACCESS_METHODS.md)
- [CONTRACTS.md](CONTRACTS.md)
- [EXPANSION_RESEARCH.md](EXPANSION_RESEARCH.md)

## Role In The Monorepo

`wowhead` remains the most mature service CLI and should become the first consumer of shared monorepo libraries.

It should keep its current command surface stable while shared infrastructure is extracted around it.

## Current Strengths

- page extraction and metadata parsing
- guide bundle export and query workflows
- local bundle inspection and refresh
- cache layers and cache inspection
- search and resolve patterns

## Current Surface

Implemented now:
- `search`
- `resolve`
- `news`
- `news-post`
- `blue-tracker`
- `blue-topic`
- `guides <category>`
- `talent-calc`
- `talent-calc-packet`
- `profession-tree`
- `dressing-room`
- `profiler`
- `entity`
- `entity-page`
- `comments`
- `compare`
- `linked-graph`
- `guide`
- `guide-full`
- `guide-export`
- `guide-query`
- `guide-bundle-*`
- `cache-inspect`
- `cache-clear`
- `cache-repair`
- `expansions`

This is strong for:
- direct entity lookup
- direct guide lookup
- comment extraction
- linked-entity traversal
- local guide bundle workflows

It is still weak or missing for:
- database-family browsing and filtering
- deeper tool decoding beyond the first tool-state slice
- deeper guide category coverage beyond the first listing surface
- deeper timeline filtering and enrichment beyond the first listing/detail summaries

Current decision:
- generic Wowhead database pages are intentionally deferred for now
- direct `entity`, `entity-page`, `comments`, `search`, `resolve`, `guide`, `news`, `blue-tracker`, and `guides <category>` are the preferred surfaces until a concrete browse/filter workflow requires more
- database-page support should only move forward when the current direct commands cannot answer a real user or agent workflow cleanly

## Quality Review Findings

The current `wowhead` CLI is more mature than the other providers, but the review surfaced several meaningful gaps:

- the CLI is still centered on direct entity pages and direct guide fetches, while live Wowhead also exposes first-class database browsing and filtering
- tool surfaces like Talent Calculator, Profession Tree Calculator, Dressing Room, and Profiler are not represented in the CLI at all
- guide support is strong for direct guide IDs and URLs, but category/index discovery like `guides/classes`, `guides/professions`, and `guides/raids` is not modeled
- there is no dedicated support for Wowhead `news` or `blue-tracker`
- Wowhead type support is spread across multiple registries, and they are already drifting

The type-registry drift was the most important structural issue to fix before adding more Wowhead features:
- search suggestions map type `112` to `companion`
- but `companion` is not consistently supported across entity parsing, hydrate support, search hints, and resolve filters

That is now refactored into one canonical internal type registry before adding more database families.

## News And Blue Tracker

Live Wowhead currently exposes:
- `https://www.wowhead.com/news`
- `https://www.wowhead.com/blue-tracker`

Current CLI state:
- there is now an explicit `news` command
- there is now an explicit `blue-tracker` command
- both commands support topic filtering plus bounded date-window scans
- both surfaces now also have detail-fetch companions:
  - `news-post`
  - `blue-topic`
- generic `search` / `resolve` are still not a reliable substitute for those surfaces

That means the trustworthy contract now covers common requests like:
- latest Wowhead news
- recent class tuning posts
- recent blue posts
- finding a blue-tracker thread by topic

Implemented direction:
- `wowhead news`
- `wowhead news-post`
- `wowhead blue-tracker`
- `wowhead blue-topic`
- support for:
  - latest listing
  - topic search
  - bounded time windows
  - explicit date cutoffs
  - pagination or capped historical slices
  - listing query provenance
  - stable listing-field filters such as author/type/region/forum
  - facet summaries across the matched timeline window
  - single post/topic fetch with citations
  - related/recent-post context on article pages when Wowhead exposes it
  - lightweight participant and blue-author summaries on blue-tracker topic pages

Still to add:
- category/filter narrowing when the live page model allows it
- deeper post/topic filtering and enrichment beyond the first detail-fetch slice

These should be treated as list/article surfaces, not forced through `guide` or `entity`.

Important usage expectation:
- agents and users will often want topic context over time, not just the newest post
- that means `news` and `blue-tracker` should be able to answer questions like:
  - posts about a topic across a long time window
  - posts between two dates
  - recent posts since a cutoff
  - historical context before and after a known change

So the design should include query fields like:
- `query`
- `date_from`
- `date_to`
- `limit`
- `page` or equivalent bounded pagination

And the response contract should expose:
- publish timestamp
- source URL
- listing query provenance
- truncation/pagination state
- enough summary metadata to build timelines without fetching every article body first

## Database And Tool Expansion

Live Wowhead exposes several surfaces that should become first-class CLI capabilities because they are more useful to agents in structured form than in a browser:

Database-family surfaces:
- `/database`
- `/items`
- `/npcs`
- `/quests`
- `/spells`
- `/achievements`
- `/zones`
- `/maps`
- `/objects`
- `/factions`
- `/currencies`
- `/skills`
- `/item-sets`
- `/followers`
- `/titles`

Tool surfaces:
- `/talent-calc`
- `/profession-tree-calc`
- `/dressing-room`
- `/list` (Profiler)

Guide-category surfaces:
- `/guides/classes`
- `/guides/professions`
- `/guides/raids`

Recommended additions:
- `wowhead db <family> ...`
- `wowhead guides <category> ...`
- `wowhead news ...`
- `wowhead blue-tracker ...`
- `wowhead talent-calc ...`
- `wowhead profession-tree ...`
- `wowhead dressing-room ...`
- `wowhead profiler ...`

Guide-category direction:
- keep `guides <category>` centered on the live guide listview data instead of browser-style scraping
- prefer stable list metadata filters such as:
  - author
  - updated window
  - patch range
- support explicit guide-list sorting such as:
  - relevance
  - updated
  - published
  - rating
- expose guide-set facet summaries so agents can inspect the filtered result bucket without opening individual guide pages

Current decision on database pages:
- do not implement generic `wowhead db <family>` yet just because the pages exist
- the current direct Wowhead commands are more reliable for most entity and guide workflows
- database pages become worth the parser complexity only for concrete bulk browsing/filtering use cases that the current commands do not cover well
- this keeps the Wowhead CLI biased toward reliable structured retrieval instead of broad but fragile page-surface coverage

Current tool state:
- `talent-calc` is now a real route-state decoder and extracts:
  - class slug
  - spec slug
  - build code
  - embedded listed builds when the page exposes them
- `talent-calc-packet` is the first exact talent transport producer on the Wowhead side:
  - it emits an exact `talent_transport_packet` only when the explicit calculator ref includes a build code
  - it keeps the cited state URL next to the packet, and includes page metadata plus listed embedded builds on a best-effort basis when page fetch succeeds
  - add `--out <path>` when you want to save just the exact packet JSON for wrapper handoff or parity checks
  - if packet validation fails, it stops with `invalid_transport_packet` before printing or writing malformed packet JSON
- `profession-tree` is now a real route-state decoder and extracts:
  - profession slug
  - loadout code
- `dressing-room` is currently a stable state inspector:
  - it normalizes share hashes and cited state URLs
  - it does not yet decode the appearance payload itself
- `profiler` is currently a stable state inspector:
  - it normalizes `list=` refs and extracts obvious list/region/realm/name parts
  - it does not yet decode the underlying profile/list contents

Maintainability boundary:
- stop at stable route-state inspection for `dressing-room` and `profiler`
- do not reverse-engineer opaque client-side state payloads just because the URLs exist
- deeper decoding here is no longer straightforward HTML or embedded-JSON extraction; it is a separate reverse-engineering project
- that work should only start with an explicit product decision and a concrete user workflow that justifies the complexity
- until then, keep these commands as reliable citation/state inspectors rather than fragile pseudo-decoders

The right implementation order is:
1. expand guide-category discovery beyond the first listing slice
2. add article/thread fetch for `news` and `blue-tracker`
3. stop tool work at the maintainability boundary for `dressing-room` and `profiler` unless a later product decision reopens it
4. revisit database browse/filter commands only when a concrete browse/filter workflow justifies them

## Search And Resolve Boundary

`wowhead` search and resolve should stay focused on discovery:
- candidate search
- conservative resolution
- follow-up command guidance

They are intentionally routing aids, not analytics answer surfaces.

That means follow-up recommendations are good when they help an agent decide between:
- `entity`
- `entity-page`
- `comments`
- `guide`
- `guide-full`

But they should not be stretched into unsupported answer synthesis beyond what the retrieved Wowhead data actually shows.

## What Should Move Out First

The first shared extractions should be infrastructure that is already generic:

- output shaping and `--fields` projection
- structured error helpers
- cache backends and TTL/config plumbing
- shared config and environment handling
- HTTP transport and retry primitives
- bundle index, freshness, and local query primitives
- command routing helpers for root-level `search` and `resolve`

## What Should Stay Wowhead-Specific

- HTML parsing rules
- page JSON extraction
- entity routing quirks
- guide-body extraction
- Wowhead-specific ranking and link normalization

## What Should Wait For A Second Consumer

- article-level content abstractions beyond raw bundle storage
- search and resolve provider interfaces
- follow-up recommendation models

Those should move only after `method` proves they are not just Wowhead behavior with different names.

## Why Wowhead Matters To The Restructure

It is the strongest reference implementation for:
- agent-first CLI output
- local bundle workflows
- cache inspection and repair

But it should not define the shape of every other service. It should validate shared infrastructure, not dictate shared content models.

## Recommended Migration Posture

- keep `wowhead` runnable during the refactor
- move code only when a second consumer exists or the code is obviously generic
- use `wowhead` as the first backend behind `warcraft`

## Source Links

- [Usage](../USAGE.md)
- [Access Methods](ACCESS_METHODS.md)
- [Expansion Research](EXPANSION_RESEARCH.md)

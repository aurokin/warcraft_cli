# Raider.IO CLI

## Status

`raiderio` is now implemented as a phase-1 provider.

Current command surface:
- `raiderio doctor`
- `raiderio search`
- `raiderio resolve`
- `raiderio character`
- `raiderio guild`
- `raiderio mythic-plus-runs`
- `raiderio sample mythic-plus-runs`
- `raiderio sample mythic-plus-players`
- `raiderio distribution mythic-plus-runs`
- `raiderio distribution mythic-plus-players`
- `raiderio threshold mythic-plus-runs`
- `raiderio leaderboard mythic-plus`

Current quality notes:
- structured `region realm name` queries now use direct profile probes before falling back to the weaker site search route
- live search/resolve quality is materially better for exact guild and character lookups
- the first sample-backed analytics primitives now exist for Mythic+ run sampling and run-derived distributions
- sampled run snapshots now preserve roster class/spec data so composition analysis does not need to rediscover it later
- the first threshold-style primitive now exists for sampled Mythic+ run score and level estimation
- sampled run analytics now support explicit post-sample filters for level, score, roster role, class, spec, and player region
- filtered outputs preserve source-run counts and excluded-run counts so narrower slices stay provenance-safe
- deduped player snapshots now exist on top of sampled runs, so agents can work with participant-level appearance and class/spec slices instead of only raw run rows
- player-snapshot analytics now report truncation explicitly when `--player-limit` cuts the deduped participant set
- a thin season-scoped `leaderboard mythic-plus` view now exists over the same sampled-run primitive, with explicit `resolved_season`, sampled freshness, and leaderboard citations
- every Mythic+ analytics command echoes an explicit `resolved_season` in its `query` block; `--season current` (or empty) resolves to the Raider.IO current default season and the effective slug is recovered from the API response
- broader Mythic+ surfaces and richer profile workflows are still later-phase work

## Next Quality Direction

The next Raider.IO work should not focus on one-off question commands.

It should focus on reusable analytics systems that let agents answer ranking and season questions reliably.

Examples of the kinds of questions this should eventually support:
- most common or most successful class/spec slices in a season
- common group compositions for a dungeon or score bracket
- score-to-key-level estimation
- distribution and threshold questions like what 3k rating usually looks like

The important point is that the CLI does not need to answer those questions directly in one leap.
It needs to provide trustworthy building blocks first.

## Why Raider.IO Is Different

`raiderio` should be treated as an API-first integration, not a scraping project.

The official developer API is documented and exposes an OpenAPI surface. That should be the primary integration path.

## Research Summary

Observed from official developer materials:
- developer docs are served from `https://raider.io/api`
- OpenAPI is published at `https://raider.io/openapi.json`
- the sampled OpenAPI document reported version `0.62.5`
- the sampled OpenAPI document exposed 35 paths
- visible endpoint groups include character, guild, raiding, mythic plus, and live tracking
- the official API description states unauthenticated requests are limited to 200 requests per minute
- the official description prohibits automated scraping beyond the published endpoints

## Access Model

This should be an API-first service:
- typed request builders
- response normalization where helpful
- cache-aware profile and leaderboard fetches
- unauthenticated phase 1 support first
- optional app-key support deferred to a later phase

## Likely CLI Shape

- `raiderio doctor`
- `raiderio search "<query>"`
- `raiderio resolve "<query>"`
- `raiderio character <region> <realm> <name>`
- `raiderio guild <region> <realm> <name>`
- `raiderio mythic-plus-runs ...`

## What Can Reuse Shared Code

- HTTP client and retry primitives
- cache backends and TTL handling
- output shaping
- auth/config handling where needed

Potential future shared analytics pieces:
- sampling contracts
- normalized snapshot contracts
- provenance and confidence metadata
- freshness metadata for derived analytics

## What Should Not Be Forced Into Shared Models

- Raider.IO endpoint shapes should not be turned into a universal Warcraft entity contract
- Raider.IO ranking logic should not be treated as the generic search ranking layer for the repo

## What Should Stay Raider.IO-Specific

- endpoint catalog
- typed field selection
- rate-limit aware request policy
- response models and ranking rules
- season-specific Mythic+ analytics query builders
- Raider.IO-specific normalization from raw profile and run payloads into analytics snapshots

## First Useful Slice

1. character profile lookup
2. guild profile lookup
3. one mythic-plus runs path
4. `doctor`
5. conservative search/resolve built on the live site search surface

## Analytics Systems To Build

These are the next high-value systems for Raider.IO.

### Sampling

Build reliable sampling over:
- Mythic+ run leaderboards
- current-season profile slices
- class/spec filtered slices when the provider surface allows it

This is the base system for any popularity, threshold, or distribution question.

### Normalized Snapshots

Add Raider.IO-specific normalized records for:
- character snapshot
- guild snapshot
- run snapshot
- group composition snapshot
- score snapshot

These should stay Raider.IO-specific initially, but the output contract should be stable enough for later cross-provider analytics.

### Aggregation

Build simple, trustworthy aggregation helpers for:
- averages
- medians
- percentiles
- frequency counts
- top-N combinations
- threshold estimation

This is the right layer for questions like:
- average dungeon level near 3k rating
- most common group composition at a rating bracket
- most common spec appearances in a sampled slice

### Provenance

Every analytic payload should carry:
- source provider
- query slice
- sample size
- season
- time/freshness
- exclusions or caveats

Without that, agents will over-trust thin or biased samples.

### Freshness

Analytics answers are more time-sensitive than direct profile lookups.

Derived outputs should include:
- sampled_at
- season context
- freshness windows
- cache policy appropriate for leaderboard-style data

## Suggested Next Commands

The next commands should be system-building commands, not freeform answer commands.

Good candidates:
- `raiderio leaderboard ...`
- `raiderio sample ...`
- `raiderio distribution ...`
- `raiderio threshold ...`

Current implemented subset:
- `raiderio leaderboard mythic-plus`
- `raiderio sample mythic-plus-runs`
- `raiderio distribution mythic-plus-runs`
- `raiderio threshold mythic-plus-runs`

Examples (match shipped command shapes):
- `raiderio leaderboard mythic-plus --season current --region us --dungeon all --limit 20`
- `raiderio sample mythic-plus-runs --region us --limit 100`
- `raiderio distribution mythic-plus-runs --metric mythic_level --season current`
- `raiderio threshold mythic-plus-runs --metric score --value 3000`

The exact names matter less than keeping the outputs:
- structured
- sample-backed
- freshness-aware
- provenance-rich

## Deferred To Later Phases

- app-key / elevated-rate-limit support
- stronger realm/region-aware discovery heuristics if the current search surface proves too shallow
- broader leaderboard coverage
- live-tracking endpoints
- cross-provider talent/build analytics that really require Blizzard API or another source
- encounter recommendation workflows that belong more naturally to Warcraft Logs plus article providers

## Risks

- region/realm/name resolution needs clear CLI ergonomics
- live site search is usable now, but it is not part of the documented `/api/v1` developer surface
- auth/app-key support should not distort the initial provider shape
- rate-limit policy needs to be explicit from day one

## Source Links

- `https://raider.io/api`
- `https://raider.io/openapi.json`
- [Roadmap](../ROADMAP.md)

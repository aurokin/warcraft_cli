# WowProgress CLI

## Status

`wowprogress` is now implemented as a working rankings/profile provider with its first sample-backed analytics primitives.

Current command surface:
- `wowprogress doctor`
- `wowprogress search`
- `wowprogress resolve`
- `wowprogress guild`
- `wowprogress guild-history`
- `wowprogress guild-ranks`
- `wowprogress guild-snapshot`
- `wowprogress history-trajectory`
- `wowprogress character`
- `wowprogress leaderboard`
- `wowprogress sample`
- `wowprogress distribution`
- `wowprogress threshold`

Current quality notes:
- structured guild and character lookups are the primary search and resolve path
- direct route probes now treat canonical WowProgress realm forms like `US-Area 52` as exact matches for structured inputs like `area-52`
- exact short-name structured queries like `guild us area-52 xD` now resolve confidently instead of failing low-score conservatively
- structured realm normalization now tolerates some natural multi-word forms like `area 52`
- trailing unsupported qualifier terms are excluded explicitly and surfaced back as query-normalization metadata
- live guild and character retrieval coverage now validates rank extraction as well as basic profile parsing
- provider-local live coverage now exists for structured search, resolve, and leaderboard contracts
- PvE leaderboard sampling, distributions, and threshold estimates now expose freshness, provenance, and explicit caveats instead of pretending to be direct smart-answer surfaces
- top-leaderboard guild-profile sampling now combines leaderboard context with direct guild-page data so agents can inspect progression, item level, and encounter detail without manually opening multiple browser pages
- leaderboard and guild-profile samples now report sampling boundaries explicitly instead of implying complete visibility beyond the requested top slice
- guild-profile analytics now support explicit post-sample filtering for faction, difficulty, world rank, item level, and encounter slices
- guild history across tiers is now a first-class surface, built from the public `rating.tierNN` guild pages instead of forcing agents to scrape raw HTML manually
- direct guild and character lookups now use shared normalization for region, realm, and name input, including common realm variants like `Mal'Ganis` and `area 52`
- `guild-snapshot` composes current progress, world/region/realm ranks, item level, encounter summary, and a per-tier rank series into one provider-local payload with citations and freshness (no duplicate page fetch)
- `history-trajectory` reports tier-over-tier rank and item-level deltas (each tier carrying its `page_url`), with an explicit `improved` flag and a caveat that consecutive tiers are different raids/difficulties — descriptive movement, not a normalized metric
- raw `guild-history` rows now carry the parsed difficulty token (e.g. `M`) instead of the full `8/8 (M)` summary string

## Why Add It

`wowprogress` adds a different kind of value from guide and wiki sources: guild progression, character rankings, roster context, and recruitment-style profile discovery.

It overlaps somewhat with `raiderio`, but not enough to skip. The overlap is useful because it will force us to prove which profile/ranking abstractions are genuinely shared.

## Research Summary

Observed from live pages:
- direct HTML fetch works without browser automation
- current raid progression is visible in server-rendered guild ranking pages
- character ranking pages expose many sortable profile-style metrics
- the site is heavily leaderboard-oriented and filter-heavy

Sample observations from `https://www.wowprogress.com/` and `https://www.wowprogress.com/char_level/us`:
- current raid progression is listed directly in HTML
- guild, realm, and region context are visible
- character pages and ranking pages expose progression-style metrics and profile links

## Access Model

This is now treated as a rankings/profile service using browser-fingerprint HTTP fetches:
- fetch guild, character, and leaderboard HTML directly
- extract guild, character, realm, and progression context
- cache leaderboard and profile pages because the pages are expensive and fast-moving
- use the site-native `u_search` route conservatively for structured guild/character discovery
- avoid promising broad free-text discovery while the public search surface remains constrained

## Current CLI Shape

- `wowprogress doctor`
- `wowprogress search "<query>"`
- `wowprogress resolve "<query>"`
- `wowprogress guild <region> <realm> <name>`
- `wowprogress guild-history <region> <realm> <name>`
- `wowprogress guild-ranks <region> <realm> <name>`
- `wowprogress guild-snapshot <region> <realm> <name>`
- `wowprogress history-trajectory <region> <realm> <name>`
- `wowprogress character <region> <realm> <name>`
- `wowprogress leaderboard pve <region> [--realm <realm>]`
- `wowprogress sample pve-leaderboard --region <region> [--realm <realm>]`
- `wowprogress sample pve-guild-profiles --region <region> [--realm <realm>]`
- `wowprogress distribution pve-leaderboard --region <region> --metric <metric> [--realm <realm>]`
- `wowprogress distribution pve-guild-profiles --region <region> --metric <metric> [--realm <realm>]`
- `wowprogress threshold pve-leaderboard --region <region> --metric <metric> --value <value> [--realm <realm>]`
- `wowprogress threshold pve-guild-profiles --region <region> --metric <metric> --value <value> [--realm <realm>]`

## What Can Reuse Shared Code

- cache and HTTP infrastructure
- shared output shaping
- wrapper provider contract
- search and resolve payload contracts

## What Should Stay Service-Specific

- HTML parsing rules
- filter and ranking semantics
- guild/character identifier resolution
- site-specific leaderboard normalization
- leaderboard analytics semantics

## What This Service Has Validated

- whether profile and leaderboard payloads can share any contract with `raiderio`
- whether cross-source guild/character resolution belongs in shared code or only in the wrapper
- that a browser-fingerprint HTTP transport is enough for a real no-auth WowProgress provider without adding a browser-runtime dependency
- that leaderboard analytics can stay useful and trustworthy when they are framed as sampled primitives instead of fake direct answers
- that enriching a sampled leaderboard slice with direct guild-profile fetches can make the CLI materially more complete than a single browser page while still preserving explicit sample boundaries

## Risks

- the site is old and filter-heavy, so HTML stability may be inconsistent
- rankings are time-sensitive and may need careful cache policy
- some useful pages may not map cleanly to stable identifiers
- discovery remains intentionally structured because the public search surface is less reliable than direct profile and leaderboard routes

## Source Links

- `https://www.wowprogress.com/`
- `https://www.wowprogress.com/char_level/us`
- [Roadmap](../ROADMAP.md)

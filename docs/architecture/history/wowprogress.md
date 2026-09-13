# WowProgress Provider: Design Record

Historical design notes for the `wowprogress` provider. For what the CLI does today, see
[docs/wowprogress/README.md](../../wowprogress/README.md).

## Why it was added

WowProgress adds a different kind of value from guide and wiki sources: guild progression,
character rankings, roster context, and recruitment-style profile discovery. It overlaps with
`raiderio`, and that overlap was deliberate: it forced the repo to prove which profile/ranking
abstractions are genuinely shared and which must stay provider-local.

## Research summary (pre-implementation)

Observed from live pages (`https://www.wowprogress.com/`, `https://www.wowprogress.com/char_level/us`):

- direct HTML fetch works without browser automation
- current raid progression is listed directly in server-rendered guild ranking pages
- character ranking pages expose many sortable profile-style metrics
- guild, realm, and region context are visible in the leaderboard markup
- the site is old, heavily leaderboard-oriented, and filter-heavy

## What stayed shared vs provider-local

Shared: cache and HTTP infrastructure, output shaping, the wrapper provider contract, and the
search/resolve payload contracts. Provider-local: HTML parsing rules, filter and ranking semantics,
guild/character identifier resolution, site-specific leaderboard normalization, and leaderboard
analytics semantics.

## What the provider validated

- profile and leaderboard payloads can share a contract with `raiderio` only at the envelope level
- cross-source guild/character resolution belongs in the wrapper, not in shared code
- a browser-fingerprint HTTP transport is enough for a real no-auth WowProgress provider without
  adding a browser-runtime dependency
- leaderboard analytics stay trustworthy when framed as sampled primitives instead of direct answers
- enriching a sampled leaderboard slice with direct guild-profile fetches beats a single browser
  page while still preserving explicit sample boundaries

## Known risks accepted at the time

- the site is old and filter-heavy, so HTML stability may be inconsistent
- rankings are time-sensitive and need careful cache TTLs
- some useful pages do not map cleanly to stable identifiers
- discovery stays intentionally structured because the public search surface is less reliable than
  direct profile and leaderboard routes

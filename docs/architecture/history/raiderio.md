# Raider.IO Provider (Design Record)

Pre-implementation research and design intent for the `raiderio` provider, preserved for provenance.
For what the CLI does today, read [../../raiderio/README.md](../../raiderio/README.md).

## Research Summary (sampled from official developer materials)

- developer docs are served from `https://raider.io/api`
- OpenAPI is published at `https://raider.io/openapi.json`
- the sampled OpenAPI document reported version `0.62.5` and exposed 35 paths
- visible endpoint groups include character, guild, raiding, mythic plus, and live tracking
- unauthenticated requests are limited to 200 requests per minute
- the official description prohibits automated scraping beyond the published endpoints

## Access Model

Raider.IO was treated as an API-first integration rather than a scraping project: typed request
builders, response normalization where helpful, cache-aware profile and leaderboard fetches, and
unauthenticated phase-1 support, with app-key support deferred.

## Analytics Direction

The provider deliberately builds reusable analytics primitives instead of one-off question commands:

- **Sampling** over Mythic+ run leaderboards as the base for popularity, threshold, and distribution
  questions.
- **Normalized snapshots** (character, guild, run, composition, score) that stay Raider.IO-specific
  but keep a stable output contract.
- **Aggregation** helpers for averages, medians, frequency counts, top-N combinations, and threshold
  estimation, so questions like "average dungeon level near 3k rating" compose from primitives.
- **Provenance and freshness** on every derived payload (provider, query slice, sample size, season,
  `sampled_at`, caveats) so agents cannot over-trust thin or biased samples.

## Boundaries

- Raider.IO endpoint shapes are not turned into a universal Warcraft entity contract, and its ranking
  logic is not the repo's generic search ranking layer.
- Endpoint catalog, typed field selection, rate-limit policy, ranking rules, and season-specific
  Mythic+ query builders stay Raider.IO-specific.
- HTTP/retry primitives, cache backends and TTLs, output shaping, and auth/config handling come from
  the shared packages.

## Deferred

- app-key / elevated-rate-limit support
- stronger realm/region-aware discovery heuristics if the site search surface proves too shallow
- broader leaderboard coverage and live-tracking endpoints
- cross-provider talent/build analytics that need the Blizzard API or another source

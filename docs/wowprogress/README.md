# WowProgress CLI

`wowprogress` reads WowProgress guild pages, character pages, and PvE leaderboards, and turns them
into JSON for agents. It needs no credentials.

Every command emits the shared envelope (`ok`, `provider`, `command`, `kind`, `schema_version`,
`query`, `provenance`, `data`, `error`) and the exit codes defined in
[docs/foundation/ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md). Payload keys are also still
copied next to the envelope keys for existing agents; those top-level copies are deprecated, read
`data` instead. Global flags (`--pretty`, `--compact`, `--compact-max-chars`, `--fields`,
`--fields-strict`, `--profile`) go before the subcommand.

## Commands

| Command | What it returns |
| --- | --- |
| `wowprogress doctor` | Transport mode, impersonation profile, cache configuration, capabilities |
| `wowprogress search "<query>"` | Ranked guild/character route probes for a structured query |
| `wowprogress resolve "<query>"` | The single next command when one candidate is unambiguous |
| `wowprogress guild <region> <realm> <name>` | One guild page: progress, ranks, item level, encounters |
| `wowprogress guild-history <region> <realm> <name>` | Every archived `rating.tierNN` page for the guild |
| `wowprogress guild-ranks <region> <realm> <name>` | Per-tier world/region/realm ranks only |
| `wowprogress guild-snapshot <region> <realm> <name>` | Current progress + ranks + item level + encounters + rank series |
| `wowprogress history-trajectory <region> <realm> <name>` | Tier-over-tier rank and item-level deltas |
| `wowprogress character <region> <realm> <name>` | One character page: class, item level, SimDPS, PvE score |
| `wowprogress leaderboard pve <region>` | One PvE leaderboard page |
| `wowprogress sample pve-leaderboard` | Sampled leaderboard rows with sampling boundaries |
| `wowprogress sample pve-guild-profiles` | Sampled leaderboard rows enriched with their guild pages |
| `wowprogress distribution pve-leaderboard` | One metric distributed across a sampled leaderboard slice |
| `wowprogress distribution pve-guild-profiles` | One metric distributed across sampled guild profiles |
| `wowprogress threshold pve-leaderboard` | Where a target value sits in a sampled leaderboard slice |
| `wowprogress threshold pve-guild-profiles` | Where a target value sits in a sampled guild-profile slice |

### Flags

- `search`, `resolve`: `--limit` (1-50, default 5).
- `leaderboard`: `--realm`, `--limit` (1-100, default 25). Only the `pve` kind exists; anything else
  exits 2 with `invalid_query`.
- `sample pve-leaderboard`: `--region` (required), `--realm`, `--limit` (1-100, default 25).
- `distribution pve-leaderboard`: `--region` (required), `--realm`, `--limit` (1-100, default 50),
  `--metric` one of `progress`, `difficulty`, `realm`, `bosses_killed`, `rank` (default `progress`).
- `threshold pve-leaderboard`: the `distribution` flags plus `--value` (required), `--nearest`
  (1-50, default 10), and `--metric` one of `rank`, `bosses_killed` (default `rank`).
- `sample pve-guild-profiles`: `--region` (required), `--realm`, `--limit` (1-25, default 10) and
  the post-sample filters `--faction`, `--difficulty`, `--world-rank-min`, `--world-rank-max`,
  `--item-level-min`, `--item-level-max`, `--encounter` (`--faction`, `--difficulty`, and
  `--encounter` are repeatable).
- `distribution pve-guild-profiles`: the sample flags plus `--metric` one of `progress`, `faction`,
  `item_level_average`, `world_rank`, `encounter` (default `progress`).
- `threshold pve-guild-profiles`: the sample flags plus `--value` (required), `--nearest` (1-25,
  default 5), and `--metric` one of `world_rank`, `item_level_average` (default `world_rank`).

## Search and resolve behaviour

Discovery is structured, not free text. `search` accepts `<region> <realm> <name>` with an optional
leading `guild`/`character` token; anything it cannot parse returns a zero-result payload with
`suggested_queries` instead of guessing. Realm variants (`Mal'Ganis`, `area 52`, `area-52`) and
canonical forms like `US-Area 52` are treated as the same realm. Trailing recruitment-style terms
(`recruiting`, `roster`, ...) are dropped and reported back in `excluded_terms` plus a
`normalization_hint`. `resolve` only sets `resolved: true` when the top candidate scores well ahead
of the runner-up and is not ambiguous between a guild and a character.

## Transport and browser impersonation

WowProgress sits behind Cloudflare bot protection: the profile and leaderboard routes this
provider needs answer default Python HTTP clients with a `Just a moment...` challenge page instead
of the page content. The client therefore fetches through `curl_cffi` with the `chrome136`
impersonation profile (`DEFAULT_IMPERSONATE` in
`packages/wowprogress-cli/src/wowprogress_cli/client.py`), which reproduces a Chrome 136 TLS and
HTTP/2 fingerprint. This is the one sanctioned exception to the repo's transport rules; see
[docs/foundation/OPERATIONAL_BOUNDARIES.md](../foundation/OPERATIONAL_BOUNDARIES.md).

Rationale and limits:

- only public, unauthenticated, server-rendered pages are fetched; nothing is logged into and no
  paywall or private view is bypassed
- requests are read-only `GET`s and go through the shared per-host rate limiter
  (`warcraft_api.http.DEFAULT_RATE_LIMITER`, `WARCRAFT_HTTP_MIN_INTERVAL_SECONDS`, default 0.25s)
- pages are cached on disk so repeated agent turns do not re-fetch (see below)
- when Cloudflare still serves a challenge page the client fails with the `blocked` error code and
  exit 5 instead of retrying around the block

### Current status

As of 2026-09-13 every WowProgress route answers with a Cloudflare managed challenge
(`cf-mitigated: challenge`) for every `curl_cffi` impersonation profile and for a real headless
Chrome, so the provider fails with `blocked` on every network command and the `warcraft guild`,
`guild-history`, and `guild-ranks` composites lose their WowProgress source. TLS fingerprinting alone
no longer passes; restoring the provider needs a different transport (a challenge-solving browser
session or another data source). Until then, run the end-to-end suite with
`WARCRAFT_E2E_SKIP=wowprogress`, and treat the provider's `supported` tier as aspirational.

## Caching

Guild, character, and leaderboard HTML are cached through `warcraft_api.cache`:

| Variable | Default |
| --- | --- |
| `WOWPROGRESS_CACHE_BACKEND` | `file` (`none` disables caching) |
| `WOWPROGRESS_CACHE_DIR` | provider cache root `/wowprogress/http` |
| `WOWPROGRESS_GUILD_CACHE_TTL_SECONDS` | 900 |
| `WOWPROGRESS_CHARACTER_CACHE_TTL_SECONDS` | 900 |
| `WOWPROGRESS_LEADERBOARD_CACHE_TTL_SECONDS` | 300 |
| `WOWPROGRESS_REDIS_URL` / `WOWPROGRESS_REDIS_PREFIX` | unset / `wowprogress_cli` (used when the backend is `redis`) |

`doctor` reports the resolved cache settings. Sampled payloads report `freshness.cache_ttl_seconds`
as `null` when caching is off, so they never claim a TTL that is not applied.

## Analytics caveats

Sampled surfaces describe the slice they fetched and nothing beyond it: every payload carries
`sample.sampling` (requested limit, returned count, skipped rows, source scope), `freshness`, and
`citations`. `history-trajectory` compares consecutive tiers, which are different raids and
difficulties, so its deltas are descriptive movement rather than a normalized skill metric. See
[docs/foundation/SAFE_ANALYTICS_RULES.md](../foundation/SAFE_ANALYTICS_RULES.md).

## Related

- [Design record](../architecture/history/wowprogress.md)
- [Roadmap](../ROADMAP.md)

# Blizzard API Provider: Design Record

One-off design record for the `blizzard-api` provider. Context and provenance only; for what the CLI
does today see [docs/blizzard-api/README.md](../../blizzard-api/README.md).

## Why It Was Added

`blizzard-api` gives the monorepo a canonical official source for supported World of Warcraft game
data and profile data, so some lookups can prefer the authoritative source before community mirrors.
It was also chosen as the second validation point for the shared OAuth-oriented auth architecture
after `warcraftlogs` (see [AUTH_ARCHITECTURE.md](../AUTH_ARCHITECTURE.md)).

## Research Summary (at design time)

- Blizzard directs developers to the Battle.net developer portal for API documentation and auth flows.
- World of Warcraft support includes both Game Data and Profile API families.
- OAuth is a first-class requirement, including server-to-server authentication flows.
- The API ecosystem is region- and namespace-aware, which makes it structurally different from guide
  and ranking sites.

## Access Model

Treated as an official authenticated API service: authenticate with OAuth, call documented Game Data
and Profile endpoints, model region and namespace explicitly, cache within policy.

## Originally Sketched CLI Shape

The design sketch listed `doctor`, `search`, `resolve`, `item`, `spell`, `character`, `realm`,
`connected-realm`, and `auction-house`, with a deliberately narrower first slice: `doctor`, auth
verification, one game-data lookup, one profile lookup. The shipped slice is that narrow one
(`doctor`, `realm`, `item`, `character`), plus `search`/`resolve` as structured coming-soon stubs.
`spell`, `connected-realm`, and `auction-house` were never built.

## Division of Reuse

Reused from shared packages: HTTP infrastructure and retries, credential discovery, token/state
persistence, output shaping, the CLI scaffolding and error contract, the wrapper provider contract.

Kept provider-local: Battle.net OAuth token handling, region and namespace rules, endpoint models and
query builders, official API error normalization.

## Known Risks Recorded at Design Time

- Auth and namespace complexity is materially higher than the guide/ranking providers.
- Some natural-language searches may not map cleanly to official endpoints without local lookup
  assistance, which is why `search`/`resolve` were deferred rather than guessed at.
- Official API policy constraints should drive cache behavior, not the other way around.

## Source Links

- `https://develop.battle.net/`
- `https://github.com/Blizzard/api-wow-docs`
- `https://worldofwarcraft.blizzard.com/en-us/news/15336025`

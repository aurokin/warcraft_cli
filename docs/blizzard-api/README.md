# Blizzard API CLI

## Why Add It

`blizzard-api` should exist because it gives us the canonical official source for supported World of Warcraft game data and profile data.

That matters because:
- some lookups should prefer the authoritative source before community mirrors
- this provider will force us to validate OAuth, region handling, namespace handling, and official-source routing in the monorepo

## Research Summary

Current official signals:
- Blizzard directs developers to the Battle.net developer portal for API documentation and auth flows
- World of Warcraft support includes both Game Data and Profile API families
- OAuth is a first-class requirement, including server-to-server authentication flows
- the API ecosystem is region- and namespace-aware, which makes it structurally different from guide and ranking sites

## Access Model

This should be treated as an official authenticated API service:
- authenticate with OAuth
- call documented Game Data and Profile endpoints
- model region and namespace explicitly
- cache within policy and respect the official access model

Shared auth direction for this provider is defined in [AUTH_ARCHITECTURE.md](../architecture/AUTH_ARCHITECTURE.md). `blizzard-api` should be the second validation point for the shared OAuth-oriented auth architecture after `warcraftlogs`.

## Likely CLI Shape

- `blizzard-api doctor`
- `blizzard-api search "<query>"`
- `blizzard-api resolve "<query>"`
- `blizzard-api item <id-or-name>`
- `blizzard-api spell <id-or-name>`
- `blizzard-api character <realm> <name>`
- `blizzard-api realm <slug>`
- `blizzard-api connected-realm <id>`
- `blizzard-api auction-house <connected-realm-id>`

The first useful slice should stay narrower than that:
- `doctor`
- auth verification
- one game-data lookup
- one profile lookup

## Implemented

The scaffold slice (AUR-390) shipped the package, wrapper registration, and `doctor`. AUR-455 adds
live OAuth and the first read commands:

- `blizzard doctor` — reports install state, auth posture, the region/routing block, and capability
  metadata (`game_data` and `profile` are `ready`; `search`/`resolve` stay `coming_soon`).
- `blizzard realm <slug>` — dynamic Game Data namespace (`/data/wow/realm/{slug}`).
- `blizzard item <id>` — static Game Data namespace (`/data/wow/item/{id}`).
- `blizzard character <realm> <name>` — profile namespace (`/profile/wow/character/{realm}/{name}`),
  retail only.
- Each command returns `{ok, provider, command, kind, query, provenance, data}` on success and
  `{ok:false, ..., error:{code, message}}` with a nonzero exit on failure (never a traceback). With
  no credentials they return `missing_client_credentials`.
- Region routing: `--region` (default `BLIZZARD_REGION`, else `us`; supports `us`/`eu`/`kr`/`tw`/`cn`,
  with aliases like `na`). `--game-version retail|classic` (or the `--classic` shorthand) selects the
  namespace class infix. `--locale` passes through (default `en_US`, not validated).

  Auth is OAuth client-credentials, discovered in this order (matching `warcraftlogs`):
  1. repo `.env.local`
  2. `~/.config/warcraft/providers/blizzard-api.env`
  3. process environment

  Set `BLIZZARD_CLIENT_ID` and `BLIZZARD_CLIENT_SECRET`. The token is fetched once and cached in
  shared state at `~/.local/state/warcraft/providers/blizzard-api-client-credentials.json`, keyed by
  `sha256(region, id, secret)` and reused until ~60s before expiry. `doctor` never prints the secret.
- The wrapper registers `blizzard-api` with `expansion_mode=none`: Blizzard's region/namespace model
  is not the wrapper's expansion axis, so it stays out of expansion fanout.

> **Pending one-time live confirmation.** The endpoint hosts, OAuth token URL, and namespace strings
> follow documented Blizzard API conventions but have **not** been confirmed against live endpoints in
> this repo. `doctor` carries this under `region.verification`, and every command payload carries
> `provenance.verified: false`. Run
> `BLIZZARD_LIVE_TESTS=1 pytest -q -m live tests/test_blizzard_api_live.py` with real credentials to
> confirm them. CN endpoints are especially unconfirmed; classic namespace strings are best-effort.

Deferred: shared identity payloads (AUR-458); `search`/`resolve`; classic-era / Season-of-Discovery
namespaces (`classic1x`); auction-house / connected-realm / spell surfaces; user-auth flows.

## What Can Reuse Shared Code

- shared HTTP infrastructure
- cache and TTL infrastructure
- shared output shaping
- wrapper provider contract
- future shared auth/config primitives once those are proven

## What Should Stay Service-Specific

- OAuth token handling
- region and namespace rules
- endpoint models and query builders
- official API error normalization

Recommended auth posture:
- reuse shared credential discovery
- reuse shared token/state persistence helpers once implemented
- keep Battle.net OAuth, scopes, and namespace/region behavior provider-local

## What This Service Should Validate

- auth/config patterns for official APIs
- region and namespace handling in shared infrastructure
- when the wrapper should prefer official Blizzard data over community sources

## Risks

- auth and namespace complexity is materially higher than our current providers
- some natural-language searches may not map cleanly to official endpoints without local lookup assistance
- official API policy constraints should drive cache behavior, not the other way around

## Source Links

- `https://develop.battle.net/`
- `https://github.com/Blizzard/api-wow-docs`
- `https://worldofwarcraft.blizzard.com/en-us/news/15336025`
- [Roadmap](../ROADMAP.md)

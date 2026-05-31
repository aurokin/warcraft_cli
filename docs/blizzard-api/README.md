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

## Implemented (scaffold)

The first slice (AUR-390) ships the package, wrapper registration, and `doctor` only:

- `blizzard doctor` — reports install state, auth posture, a deferred region block, and honest capability metadata.
- Routed through the wrapper as `warcraft blizzard doctor`.
- Auth is OAuth client-credentials. Credentials are discovered in this order (matching `warcraftlogs`):
  1. repo `.env.local`
  2. `~/.config/warcraft/providers/blizzard-api.env`
  3. process environment

  Set `BLIZZARD_CLIENT_ID` and `BLIZZARD_CLIENT_SECRET`. `doctor` reports whether credentials are configured and where they came from; it never prints the secret. Token/state persistence uses `warcraft_core.auth` at `~/.local/state/warcraft/providers/blizzard-api.json`.
- Region is region/namespace-aware by design but **routing is deferred**: `doctor` surfaces `BLIZZARD_REGION` if set, but no endpoint uses it yet.
- The wrapper registers `blizzard-api` with `expansion_mode=none` (deferred) so it does not join expansion fanout until endpoint routing exists.

Deferred to a follow-up: the live OAuth token exchange, one Game Data + one Profile endpoint, region/namespace routing, and contract fixtures.

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

# Blizzard API

## Best For

- authoritative World of Warcraft data straight from the official Battle.net API
- realm and item records from the Game Data APIs
- character profiles from the Profile API
- a canonical source to cross-check community providers

## Start With

- readiness + auth/region posture: `warcraft blizzard doctor`
- a realm: `warcraft blizzard realm <slug>` (e.g. `illidan`)
- an item: `warcraft blizzard item <id>` (e.g. `19019`)
- a character: `warcraft blizzard character <realm> <name>` (retail only)

## Auth

- needs OAuth client credentials: set `BLIZZARD_CLIENT_ID` and `BLIZZARD_CLIENT_SECRET`
  (discovered from `.env.local`, the provider env file, or the environment)
- with no credentials, commands return a clean `missing_client_credentials` error; `doctor`
  reports whether they are configured

## Effective Use

- pick a region with `--region` (`us`/`eu`/`kr`/`tw`/`cn`, plus aliases like `na`); defaults to
  `BLIZZARD_REGION`, else `us`
- use `--game-version classic` (or the `--classic` shorthand) for classic namespaces; character
  profiles are retail-only
- `--locale` passes through (default `en_US`)
- every payload carries `provenance` (region, namespace, namespace class, source URL) and
  `provenance.verified: false` — the endpoint hosts/namespaces follow documented Blizzard
  conventions but are pending a one-time live confirmation, so treat results as best-effort
  until verified
- prefer Blizzard for the official record; prefer community providers for analytics, rankings,
  and guide content

## Boundaries

- `search` / `resolve` are coming soon: the wrapper does not fan out discovery to `blizzard-api`
  yet — use it with an explicit realm/item/character
- registered with `expansion_mode=none`: region/namespace routing is not the wrapper's expansion
  axis, so `blizzard` stays out of `--expansion` fanout

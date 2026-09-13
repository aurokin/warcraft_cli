# Blizzard API CLI (`blizzard`)

**Tier: experimental — unverified.** The endpoint hosts, OAuth token URL, and namespace strings
follow documented Blizzard API conventions but have never been confirmed against live endpoints in
this repo. Every read command (`realm`, `item`, `character`) carries `provenance.verified: false`,
and `doctor` reports `data.tier: "experimental"` plus a `region.verification` block. Treat results as unconfirmed until
someone runs the live suite with real credentials:

```bash
BLIZZARD_LIVE_TESTS=1 pytest -q -m live tests/test_blizzard_api_live.py
```

CN endpoints are especially unconfirmed; classic namespace strings are best-effort.

## What It Does

`blizzard` reads the official Battle.net World of Warcraft Game Data and Profile APIs over OAuth
client credentials and emits the shared JSON envelope.

| Command | Behavior |
|---------|----------|
| `blizzard doctor` | Reports install state, auth posture, region routing, capability metadata, and the experimental tier. |
| `blizzard realm <slug>` | Reads `/data/wow/realm/{slug}` from the dynamic Game Data namespace. |
| `blizzard item <item-id>` | Reads `/data/wow/item/{id}` from the static Game Data namespace. |
| `blizzard character <realm-slug> <name>` | Reads `/profile/wow/character/{realm}/{name}` from the profile namespace. Retail only. |
| `blizzard search <query>` | Coming soon. Returns a `kind: "coming_soon"` envelope with exit 0, not an error. |
| `blizzard resolve <query>` | Coming soon. Returns a `kind: "coming_soon"` envelope with exit 0, not an error. |

`search` and `resolve` accept `--limit` (1-50, default 5); it is ignored until those surfaces ship.

## Global Flags

Global flags go before the subcommand. They come from the shared CLI scaffolding, so they behave the
same on every provider binary:

`--pretty`, `--compact`, `--compact-max-chars <n>`, `--fields <dot.path>`, `--fields-strict`,
`--profile agent|human|debug`.

```bash
blizzard --pretty doctor
blizzard --fields data.id,data.name item 19019
```

## Routing Flags

`realm`, `item`, and `character` accept:

| Flag | Behavior |
|------|----------|
| `--region`, `-r` | `us`, `eu`, `kr`, `tw`, `cn` (aliases such as `na` normalize). Defaults to `BLIZZARD_REGION`, else `us`. |
| `--game-version` | `retail` (default) or `classic`. Selects the namespace infix. |
| `--classic` | Shorthand for `--game-version classic`. Passing both with a conflicting value is rejected. |
| `--locale` | Passed through to Blizzard. Default `en_US`; not validated. |

Classic-era and Season of Discovery namespaces (`classic1x`) are rejected rather than guessed at.
The Profile API has no classic namespace, so `blizzard character --classic` fails with
`classic_profile_unsupported`.

## Auth

OAuth client credentials. Set `BLIZZARD_CLIENT_ID` and `BLIZZARD_CLIENT_SECRET`, discovered in this
order (matching `warcraftlogs`):

1. repo `.env.local`
2. `~/.config/warcraft/providers/blizzard-api.env`
3. process environment

`BLIZZARD_REGION` sets the default region. The token is fetched once and cached in shared state at
`~/.local/state/warcraft/providers/blizzard-api-client-credentials.json`, keyed by
`sha256(region, client id, client secret)` and reused until ~60s before expiry. `doctor` reports
whether credentials and a cached token exist and never prints the secret. Without credentials every
read command fails with `missing_client_credentials` and exit 3.

## Output

Success payloads are the shared envelope: `{ok, provider, command, kind, schema_version, query,
provenance, data}`. `data` is the raw Blizzard JSON body; `provenance` carries `region`, `namespace`,
`namespace_class`, `game_version`, `locale`, `source_url`, `verified: false`, and a
`verification_note`.

`doctor` and the coming-soon stubs additionally repeat their payload keys at the top level
(`status`, `capabilities`, `coming_soon`, ...). Those top-level copies are the pre-envelope shape and
are **deprecated**; read them from `data` instead.

Failures write an error envelope to stderr and exit with the shared codes from
[ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md): 1 generic, 2 usage, 3 auth
(`missing_client_credentials`, `auth_failed`), 4 not found, 5 network/upstream (`network_error`,
`timeout`, `upstream_error`, `rate_limited`). A transport failure never prints a traceback.

## Wrapper Integration

The wrapper registers this provider as `blizzard-api` with `expansion_mode="none"`: Blizzard routes
by region and namespace class, which is not the wrapper's expansion axis, so it stays out of
expansion fanout. A side effect shared with `simc` is that `warcraft --expansion <x> blizzard ...` is
rejected; plain `warcraft blizzard ...` works.

`blizzard_api_cli.provider.PROVIDER` is the in-process surface (`search`, `resolve`, `doctor`); it
returns envelopes and never prints.

## Not Implemented

`search`/`resolve` ranking, shared identity payloads, classic-era / Season-of-Discovery namespaces,
auction-house, connected-realm, and spell surfaces, and user-auth (authorization-code) flows.

Design rationale and the original CLI sketch live in
[docs/architecture/history/blizzard-api.md](../architecture/history/blizzard-api.md).

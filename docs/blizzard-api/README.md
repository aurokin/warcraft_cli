# Blizzard API CLI (`blizzard`)

**Tier: experimental — verified live.** The endpoint hosts, OAuth token URL, and namespace
strings were confirmed against the live API on 2026-09-13 for the `us`, `eu`, `kr`, and `tw`
regions (retail and classic Game Data, retail Profile), and on 2026-10-03 for the progression Classic,
Classic Era and Anniversary Profile and Game Data namespaces, so `realm`, `item`, and `character` carry
`provenance.verified: true` there. `cn` stays `verified: false` because its host is unreachable
from where this repo is tested. `doctor` reports `data.tier: "experimental"`, `live_confirmed`, and
the verified and unverified regions. The tier stays experimental because the command surface is
thin, not because the data is suspect. Re-verify with:

```bash
make test-e2e E2E_PATHS="tests/e2e/test_blizzard.py"
```

## What It Does

`blizzard` reads the official Battle.net World of Warcraft Game Data and Profile APIs over OAuth
client credentials and emits the shared JSON envelope.

| Command | Behavior |
|---------|----------|
| `blizzard doctor` | Reports install state, auth posture, region routing, capability metadata, cache configuration, and the experimental tier. `status` is `ready` with client credentials and `degraded` without them, when `game_data` and `profile` read `requires_client_credentials`, or when the cache config does not parse or a Redis cache backend does not answer (`cache.available: false`; an unparsable config also carries `cache.error.code: "invalid_cache_config"`). |
| `blizzard realm <slug>` | Reads `/data/wow/realm/{slug}` from the dynamic Game Data namespace. |
| `blizzard item <item-id>` | Reads `/data/wow/item/{id}` from the static Game Data namespace. |
| `blizzard character <realm-slug> <name>` | Reads `/profile/wow/character/{realm}/{name}` from the profile namespace, for every game version. Also takes `<region> <realm-slug> <name>`, the order `raiderio` and `warcraftlogs` use; a positional region that differs from `--region` is `invalid_query` (exit 2). |
| `blizzard search <query>` | Coming soon. Returns a `kind: "search_results"` envelope with `coming_soon: true`, no rows and exit 0, not an error. |
| `blizzard resolve <query>` | Coming soon. Returns a `kind: "resolve_match"` envelope with `coming_soon: true`, `confidence: "none"`, no candidates and exit 0, not an error. |

`realm` and `character` also take a realm display name or the other slug spelling (`Mal'Ganis`,
`mal-ganis`, `Tarren Mill`). Blizzard's slug drops apostrophes, keeps word breaks and keeps accented letters (`malganis`,
`tarren-mill`, `festung-der-stürme`), so a hyphenated spelling is tried as written and the joined one only after a 404; a
realm that exists under neither is `not_found` (exit 4). Blizzard slugs native-script realm names in
English (`Ревущий фьорд` is `howling-fjord`, `아즈샤라` is `azshara`), so when a name with
non-Latin letters misses, the CLI reads the region's realm index once (cached for a day), which
names every realm in every locale, and retries with the matching slug. A name two realms share
(the zh_TW `閃電之刃`) stays `not_found`. A realm with no letters or digits, or a
blank character name (or one of only dots), is `invalid_query` (exit 2) and sends no request. The
character name is sent as one URL path segment, so a `/`, `?` or `#` in it cannot reach another endpoint.

`character --section <name>` reads one linked sub-resource instead of the profile summary, whose
links need the OAuth token and so cannot be followed by hand: `pvp-summary` (honor level,
battleground stats, and the rated brackets the character has played), `pvp-bracket/<bracket>` (the
rating and season record in `2v2`, `3v3`, `rbg`, or a `shuffle-<class>-<spec>` bracket that
`pvp-summary` lists), `professions`, `collections/mounts`, `collections/pets`, and
`collections/toys`. The envelope's `kind` is `character_section` and `data` is Blizzard's raw body.
Any other value is `invalid_query` (exit 2) and sends no request. A bracket the character has not
played is `not_found` (exit 4).

`search` and `resolve` accept `--limit` (1-50, default 5); it is ignored until those surfaces ship.

## Global Flags

Global flags go before the subcommand. They come from the shared CLI scaffolding, so they behave the
same on every provider binary:

`--pretty`, `--compact`, `--compact-max-chars <n>`, `--fields <dot.path>`, `--fields-strict`,
`--profile agent|human`.

```bash
blizzard --pretty doctor
blizzard --fields data.id,data.name item 19019
```

## Routing Flags

`realm`, `item`, and `character` accept:

| Flag | Behavior |
|------|----------|
| `--region`, `-r` | `us`, `eu`, `kr`, `tw`, `cn` (aliases such as `na` normalize; `oce`/`oceanic` route to `us`, where Oceanic realms live). Defaults to `BLIZZARD_REGION`, else `us`. |
| `--game-version` | `retail` (default), `classic`, `classic-era`, or `classic-anniversary`. Selects the namespace infix: `classic` is the progression Classic line (`*-classic-<region>`, Mists of Pandaria Classic today), `classic-era` is `*-classic1x-<region>`, and `classic-anniversary` is `*-classicann-<region>`. An Era or Anniversary realm read through `classic` is `not_found` or the progression namespace's `Legacy` record. |
| `--classic` | Shorthand for `--game-version classic`. Passing both with a conflicting value is rejected. |
| `--locale` | Passed through to Blizzard. Default `en_US`; not validated. |

Any other game version (including the raw infix `classic1x`) is rejected rather than guessed at.
Season of Discovery is not verified. Both routing rejections are usage errors and exit 2.

## Auth

OAuth client credentials. Set `BLIZZARD_CLIENT_ID` and `BLIZZARD_CLIENT_SECRET`, discovered in this
order (matching `warcraftlogs`):

1. repo `.env.local`
2. `~/.config/warcraft/providers/blizzard-api.env`
3. process environment

The first source that holds both the ID and the secret supplies the pair; halves from different
sources are never combined, and `doctor` names that source in `auth.credential_source`.
`BLIZZARD_REGION` sets the default region and resolves on its own through the same order. The token is fetched once and cached in shared state at
`~/.local/state/warcraft/providers/blizzard-api-client-credentials.json`, keyed by
`sha256(region, client id, client secret)` and reused until ~60s before expiry. `doctor` reports
whether credentials and a cached token exist and never prints the secret. Without credentials every
read command fails with `missing_client_credentials` and exit 3.

## Output

Success payloads are the shared envelope: `{ok, provider, command, kind, schema_version, query,
provenance, data}`. `data` is the raw Blizzard JSON body; `provenance` carries `region`, `namespace`,
`namespace_class`, `game_version`, `locale`, `source_url`, `verified` (true for confirmed regions), a
`verification_note`, and the shared `cache` block (see
[USAGE.md](../USAGE.md#reading-cache-state-provenancecache)).

The envelope's `provider` is `blizzard-api`, the provider id the wrapper registry, `warcraft doctor`
tiers and this doc set use. The binary and the wrapper subcommand are `blizzard`
(`warcraft blizzard realm illidan`); `warcraft blizzard-api ...` is not a command. It is the only
provider whose id differs from its binary name.

## Caching

Responses are cached on disk under the XDG cache root (`blizzard-api/http`), keyed on host, path,
namespace and locale, never on the token: the static namespace (items) for 24 hours, the dynamic and
profile namespaces (realms, characters) for 15 minutes. A replay sends no request at all, not even
for a token, but still needs the credentials configured. Override with `BLIZZARD_STATIC_CACHE_TTL_SECONDS`, `BLIZZARD_DYNAMIC_CACHE_TTL_SECONDS`,
`BLIZZARD_CACHE_DIR`, or `BLIZZARD_CACHE_BACKEND=file|redis|none` (Redis takes `BLIZZARD_REDIS_URL`
and `BLIZZARD_REDIS_PREFIX`).

Failures write an error envelope to stderr and exit with the shared codes from
[ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md): 1 generic (`invalid_response`), 2 usage
(`unsupported_region`, `unsupported_game_version`, `invalid_query`), 3 auth
(`missing_client_credentials`, `auth_failed`), 4 not found, 5 network/upstream (`network_error`,
`timeout`, `upstream_error`, `rate_limited`). A transport failure never prints a traceback.

Flag validation runs before any request, so a bad `--region`/`--game-version` fails offline with
exit 2 and never spends a round trip.

## Wrapper Integration

The wrapper registers this provider as `blizzard-api` with `expansion_mode="none"`: Blizzard routes
by region and namespace class, which is not the wrapper's expansion axis, so it stays out of
expansion fanout. `warcraft --expansion <x> blizzard ...` still runs: the wrapper ignores the
expansion for this provider (Blizzard's default retail routing applies) and attaches an
`expansion_advisory` note to the result. For classic, pass the game version to the command
itself: `blizzard <cmd> ... --game-version classic|classic-era|classic-anniversary` (there is no
namespace flag).

`blizzard_api_cli.provider.PROVIDER` is the in-process surface (`search`, `resolve`, `doctor`); it
returns envelopes and never prints.

## Not Implemented

`search`/`resolve` ranking, shared identity payloads, Season of Discovery routing, PvP
leaderboards and season cutoffs, achievements and other character sub-resources beyond
`--section`, auction-house and commodity prices, connected-realm and spell surfaces, and user-auth
(authorization-code) flows.

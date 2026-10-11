# Blizzard API CLI (`blizzard`)

**Tier: supported — verified live.** The endpoint hosts, OAuth token URL, and namespace
strings were confirmed against the live API on 2026-09-13 for the `us`, `eu`, `kr`, and `tw`
regions (retail and classic Game Data, retail Profile), and on 2026-10-03 for the progression Classic,
Classic Era and Anniversary Profile and Game Data namespaces, so every read carries
`provenance.verified: true` there. The PvP, collections and auction reads were checked live on
2026-10-03; which game versions answer them is in
[Game versions](#game-versions-for-pvp-collections-and-auctions). `cn` stays `verified: false`
because its host is unreachable from where this repo is tested. `doctor` reports
`data.tier: "supported"`, `live_confirmed`, and the verified and unverified regions. The tier covers verified typed reads; free-text discovery is independently unsupported. Re-verify with:

```bash
make test-e2e E2E_PATHS="tests/e2e/test_blizzard.py"
```

## What It Does

`blizzard` reads the official Battle.net World of Warcraft Game Data and Profile APIs over OAuth
client credentials and emits the shared JSON envelope.

| Command | Behavior |
|---------|----------|
| `blizzard doctor` | Reports install state, auth posture, region routing, capability metadata, cache configuration, and the supported tier. `status` is `ready` with client credentials and `degraded` without them, when `game_data` and `profile` read `requires_client_credentials`, or when the cache config does not parse or a Redis cache backend does not answer (`cache.available: false`; an unparsable config also carries `cache.error.code: "invalid_cache_config"`). |
| `blizzard realm <slug>` | Reads `/data/wow/realm/{slug}` from the dynamic Game Data namespace. |
| `blizzard item <item-id>` | Reads `/data/wow/item/{id}` from the static Game Data namespace. |
| `blizzard character <realm-slug> <name>` | Reads `/profile/wow/character/{realm}/{name}` from the profile namespace, for every game version. Also takes `<region> <realm-slug> <name>`, the order `raiderio` and `warcraftlogs` use; a positional region that differs from `--region` is `invalid_query` (exit 2). |
| `blizzard pvp-season [season-id]` | A PvP season (the current one by default): name, start, every season id, its leaderboard brackets, and its title rating cutoffs. See [PvP](#pvp). |
| `blizzard pvp-leaderboard <bracket>` | The top ranks of one leaderboard (`--limit`, default 25; `--season`). See [PvP](#pvp). |
| `blizzard pvp-character <realm-slug> <name>` | A character's honor, battleground record, and rating and record in every bracket it has played. See [PvP](#pvp). |
| `blizzard collections <realm-slug> <name>` | A character's mounts, pets, toys, heirlooms and transmog appearances: counts plus a filtered, limited list. See [Collections](#collections). |
| `blizzard auctions <realm-slug>` | The realm's connected-realm auction house, summarized per item id. See [Auctions and commodities](#auctions-and-commodities). |
| `blizzard commodities` | The region-wide retail commodity market, summarized per item id. See [Auctions and commodities](#auctions-and-commodities). |
| `blizzard search <query>` | Unsupported. Returns `unsupported_operation` on stderr and exits 2; use explicit typed reads. |
| `blizzard resolve <query>` | Unsupported. Returns `unsupported_operation` on stderr and exits 2; use explicit typed reads. |

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

`pvp-character` and `collections` take the same `<realm> <name>` / `<region> <realm> <name>`
arguments and realm spellings as `character`; `auctions` takes the same realm spellings as `realm`.

`search` and `resolve` retain `--limit` for compatibility, but always fail explicitly. The wrapper excludes them from discovery.

## PvP

- `pvp-season [season-id]` reads the season index, the season, its leaderboard index and its reward
  index. `data` carries `season_id`, `season_name` (`null` on Classic), `season_start` (ISO 8601
  UTC), `current_season_id`, `seasons` (every id, newest first), `brackets` (the leaderboard names
  `pvp-leaderboard` takes: `2v2`, `3v3`, `rbg`, `shuffle-overall`, `blitz-overall`,
  `shuffle-<class>-<spec>`, `blitz-<class>-<spec>`, and `5v5` on Classic), and `rewards`: one row per
  title cutoff (`bracket` type, `achievement`, `achievement_id`, `rating_cutoff`, and
  `specialization`/`specialization_id` for Shuffle and Blitz or `faction` for battleground titles;
  the absent ones are `null`). Spec names repeat across classes (Frost), so match on
  `specialization_id`. Cutoffs move while a season runs; `freshness` dates them.
  Blizzard answers the oldest seasons with HTTP 403, which surfaces as `auth_failed` (exit 3).
- `pvp-leaderboard <bracket> [--season N] [--limit N]` returns `bracket`, `bracket_type`,
  `season_id`, `total_entries` (Blizzard publishes up to about 5000), `returned`, `truncated`,
  `freshness`, and `entries` of `rank`, `rating`, `name`, `realm` (slug), `character_id`, `faction`,
  `played`, `won`, `lost` and `tier_id`. `--limit` is 1-5000 (default 25). On `shuffle-overall`
  the record counts rounds, not matches (it equals the profile's `season_rounds`). A bracket that is not one lowercase path segment is
  `invalid_query` (exit 2) and sends no request; a bracket the season has no board for is `not_found`.
- `pvp-character <realm> <name>` reads `pvp-summary` and then every `pvp-bracket` it links. Off
  retail, Blizzard's `pvp-summary` leaves out brackets the character has rated (the top progression
  Classic 3v3 player's summary links only 2v2), so it also reads `2v2`, `3v3`, `5v5` and `rbg`
  and skips the ones that answer HTTP 404; per-spec Shuffle and Blitz brackets a Classic summary
  omits cannot be found that way. `data`
  carries `character` (`name`, `id`, `realm`), `honor_level`, `honorable_kills`, `battlegrounds`
  (`map`, `played`, `won`, `lost`), and `brackets`: `bracket`, `bracket_type`, `season_id` (a
  bracket from an earlier season keeps its own id), `rating`, `tier_id`, `specialization` (Shuffle
  and Blitz), `season` and `weekly` records, and for Solo Shuffle `season_rounds` and
  `weekly_rounds`. A character with no rated play has `brackets: []`.

`character --section pvp-summary` and `--section pvp-bracket/<bracket>` still return the raw bodies.

## Collections

`collections <realm> <name> [--kind K ...] [--match TEXT] [--limit N]` reads
`/collections/<kind>` for each kind (default all of `mounts`, `pets`, `toys`, `heirlooms`,
`transmogs`; `--kind` repeats). `data.character` echoes the name and the realm slug that answered,
and `data.collections.<kind>` carries `count` (the whole collection), `matched` (rows whose name
contains `--match`, case-insensitive; the whole collection without it), `returned`, `truncated`, and
`items` sorted by name, cut to `--limit` (default 20, max 5000):

| Kind | Item fields | Extra fields |
|------|-------------|--------------|
| `mounts` | `id`, `name`, `is_favorite`, `is_useable` (false for a mount this character cannot ride) | |
| `pets` | `id` (the journal pet), `name` (species), `species_id`, `nickname`, `level`, `quality`, `is_favorite` | `unique_species` |
| `toys` | `id`, `name`, `is_favorite` | |
| `heirlooms` | `id`, `name`, `upgrade_level` | |
| `transmogs` | appearance sets: `id`, `name` (`count` is the number of sets) | `appearance_count`, `appearances_by_slot` |

Single transmog appearances are counted per slot, not listed: Blizzard names them only by id. Any
other `--kind` is `invalid_query` (exit 2) and sends no request. `character --section
collections/<kind>` still returns the raw body.

## Auctions and commodities

`auctions <realm> [--item-id N ...] [--limit N]` reads the realm record, follows its connected realm,
and reads that auction house. `commodities [--item-id N ...] [--limit N]` reads the region-wide
retail commodity market (ore, herbs, reagents and other stackables are listed there, not on realms).
Neither dumps listings. `data` carries `auction_count` (listings), `item_count` (distinct items),
`freshness`, and `items` of:

- `item_id`, `auctions` (listings), `quantity` (units)
- `min_unit_price` and `median_unit_price`, in copper (10000 copper = 1 gold). The median is
  weighted by units: the lowest price at which half of the listed units are available. Realm
  listings carry a buyout for the whole listing, divided by its quantity here. Bids are not prices;
  a listing with only a bid counts toward `auctions` and `quantity` but not toward prices, so an
  item listed only that way has `null` prices.

With `--item-id` (repeatable) `items` holds exactly those ids that are listed, in the order asked,
and `not_listed` the rest. Without it, `items` is the `--limit` most-listed items (default 20, max
500) with `returned` and `truncated`. `auctions` also returns `realm` and `connected_realm_id`.

Rows are per item id, so variants of one item id merge: every battle pet is a Pet Cage (`82800`),
and gear at different item levels shares one row. Item names are not included; read them with
`blizzard item <id>`.

`freshness` is `{last_modified, age_seconds}`: `last_modified` is Blizzard's own `Last-Modified` for
the snapshot (ISO 8601 UTC; Blizzard rebuilds auctions about hourly) and `age_seconds` its age when
the command ran, so a replay from the cache still reports how old the prices are. `pvp-leaderboard`
and `pvp-season` (for its cutoffs) carry the same block.

### Game versions for PvP, collections and auctions

Checked live on 2026-10-03 and 2026-10-04: retail, progression Classic (`--classic`) and
Anniversary (`--game-version classic-anniversary`) answer `pvp-season`, `pvp-leaderboard` and
`pvp-character`; retail and progression Classic answer `auctions`. Classic Era has no PvP season
index (`not_found`). Classic Era and Anniversary connected-realm auction houses answered HTTP 404
(`not_found`). `collections` is retail-only in practice: the progression Classic profile namespace
answers `collections/mounts` with HTTP 404 (`not_found`).
Blizzard's API publishes no Classic commodity (stackable trade goods) prices: the Classic
region-wide commodity endpoint returns only links, and Classic realm auction houses list
non-commodity items only (every listing has quantity 1), so ore, cloth and herbs are absent from
`auctions <realm> --classic`. `commodities` with any game version other than retail is
`unsupported_game_version` (exit 2) and sends no request.

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

Every read accepts `--region`, `--game-version` and `--classic`; all but `pvp-leaderboard`,
`auctions` and `commodities`, whose bodies carry no translated text, also accept `--locale`:

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
provenance, data}`. For `realm`, `item`, `character` and `character --section`, `data` is the raw
Blizzard JSON body; the PvP, collections and auction reads return the compact shapes above.
`provenance` carries `region`, `namespace`, `namespace_class`, `game_version`, `locale`,
`source_url` (for reads that make several requests, the main one: the season, the leaderboard, the
pvp-summary, the first collection, the auction house), `verified` (true for confirmed regions), a
`verification_note`, and the shared `cache` block (see
[USAGE.md](../USAGE.md#reading-cache-state-provenancecache)).

The envelope's `provider` is `blizzard-api`, the provider id the wrapper registry, `warcraft doctor`
tiers and this doc set use. The binary and the wrapper subcommand are `blizzard`
(`warcraft blizzard realm illidan`); `warcraft blizzard-api ...` is not a command. It is the only
provider whose id differs from its binary name.

## Caching

Responses are cached on disk under the XDG cache root (`blizzard-api/http`), keyed on host, path,
namespace and locale, never on the token: the static namespace (items) and the PvP season,
season-detail and leaderboard indexes for 24 hours, the dynamic and profile namespaces (realms,
characters, `pvp-character`) for 15 minutes, and snapshots (auctions, commodities, leaderboards,
PvP cutoffs, collections) for 1 hour. The current season therefore follows a new season within a day.
Leaderboards, auction houses, the commodity market and transmog collections are reduced to their
compact form before they are cached, so a replay never re-reads megabytes. A replay sends no request at all, not even
for a token, but still needs the credentials configured. Override with `BLIZZARD_STATIC_CACHE_TTL_SECONDS`, `BLIZZARD_DYNAMIC_CACHE_TTL_SECONDS`, `BLIZZARD_SNAPSHOT_CACHE_TTL_SECONDS`,
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

`search`/`resolve` ranking, shared identity payloads, Season of Discovery routing, achievements and
other character sub-resources beyond `--section`, item names on auction rows, battle-pet species
and item-level variants on auction rows, Classic Era and Anniversary auction houses, Classic
commodity prices (Blizzard publishes none), a
connected-realm record surface, spell surfaces, and user-auth (authorization-code) flows.

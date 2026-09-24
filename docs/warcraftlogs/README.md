# Warcraft Logs CLI

`warcraftlogs` queries the official Warcraft Logs OAuth 2.0 + GraphQL API. It does not scrape.
It exposes typed commands for guilds, characters, rankings, reports, encounter analytics, and
sampled cross-report analytics, plus a raw `graphql` passthrough for queries no typed command covers.

Companion docs:
- [SCOPING.md](SCOPING.md) - scoping conventions and raw-GraphQL rules
- [CACHING.md](CACHING.md) - cache keys, TTLs, and derived-output trust fields
- [AUTH_ARCHITECTURE.md](../architecture/AUTH_ARCHITECTURE.md) - shared auth architecture

## Auth

Public commands use the OAuth client-credentials flow. `auth whoami` and other user-scoped
endpoints need a saved user token from the authorization-code or PKCE flow.

Credentials:
- `WARCRAFTLOGS_CLIENT_ID`
- `WARCRAFTLOGS_CLIENT_SECRET`

The first layer that holds both keys wins; an ID from one layer is never combined with a secret
from another. Layers are read purely (the process environment is never mutated), highest first:
1. repo-local `.env.local` (searched up to the enclosing git repository root)
2. `~/.config/warcraft/providers/warcraftlogs.env`
3. the process environment

Saved user-token state lives at `~/.local/state/warcraft/providers/warcraftlogs.json` (file mode
`0600`). `auth logout` deletes it.

User login is a two-step manual flow: run `auth login` (or `auth pkce-login`) with
`--redirect-uri` to get an authorize URL, then re-run the same command with `--code` and `--state`
from the callback. `--scope` (repeatable) selects the OAuth scopes: `view-user-profile` for
`currentUser` data and `view-private-reports` for private or guild-stealth reports. A callback whose
`state` or `--redirect-uri` does not match the pending flow is rejected before the code is exchanged.

`doctor` and `auth status` probe live access by default (`rate_limit()` for public access,
`current_user()` for user access). Pass `--no-live` for local readiness only.

## Site profiles

One package serves all three Warcraft Logs sites. The global `--site` flag routes the site URL,
OAuth endpoints, and both GraphQL endpoints:

| `--site` | Host |
| --- | --- |
| `retail` (default) | `www.warcraftlogs.com` |
| `classic` | `classic.warcraftlogs.com` |
| `fresh` | `fresh.warcraftlogs.com` |

```bash
warcraftlogs --site classic auth client
warcraftlogs --site fresh expansions
```

The `warcraft` wrapper maps its expansion vocabulary to these profiles: `retail` -> `retail`;
`classic`, `tbc`, `wotlk`, `cata`, `mop-classic` -> `classic`; `fresh` -> `fresh`. `ptr`, `beta`,
and `classic-ptr` are rejected rather than coerced.

## Commands

Run `warcraftlogs --help` or `warcraftlogs <command> --help` for flags. Global flags go before
the subcommand: `--site`, plus the shared output flags `--pretty`, `--compact`,
`--compact-max-chars`, `--fields`, `--fields-strict`, and `--profile`.

Discovery and health:
`search`, `resolve`, `doctor`, `rate-limit`.

`search` and `resolve` are explicit-report-only: they match a Warcraft Logs report URL or report
code and return a discovery hint for anything else.

Auth: `auth status`, `auth client`, `auth token`, `auth login`, `auth pkce-login`, `auth whoami`,
`auth logout`.

World and static metadata: `regions`, `expansions`, `server`, `zones`, `zone`, `encounter`.

Guilds: `guild`, `guild-members`, `guild-attendance`, `guild-rankings`, `guild-reports`.

Characters: `character`, `character-rankings`.

Rankings: `encounter-rankings` (official encounter leaderboard).

Reports: `reports`, `report`, `report-fights`, `report-master-data`, `report-player-details`,
`report-events`, `report-table`, `report-graph`, `report-rankings`, `graphql`.

`report-player-details` and `report-events` require the slice shapes Warcraft Logs actually
answers: `--fight-id`, or both `--start-time` and `--end-time`. Anything wider fails with
`missing_scope` (exit 2) rather than returning an empty payload with `ok: true`.

A well-formed slice that matches no fight — an unknown `--fight-id`, an `--encounter-id` or
`--difficulty` the report never pulled — makes `report-events`, `report-table`, `report-graph`,
`report-rankings`, and `report-player-details` fail with `not_found` (exit 4). Every listed
`--fight-id` has to exist: `--fight-id 1 --fight-id 9999` fails rather than answering for fight 1
alone, and `error.details.missing_fight_ids` names the ones that did not match. A request that names no fight
at all (a window, or the whole report) is left alone: an empty answer to it is a real answer.
`report-player-details` additionally fails when its window matches no fight, because a fight
Warcraft Logs has always returns a roster.

Encounter analytics (one report, one fight): `report-encounter`, `report-encounter-players`,
`report-player-talents`, `report-encounter-casts`, `report-encounter-buffs`,
`report-encounter-aura-summary`, `report-encounter-aura-compare`,
`report-encounter-damage-source-summary`, `report-encounter-damage-target-summary`,
`report-encounter-damage-breakdown`.

The aura and damage summaries emit typed rows only. `--include-raw` attaches the untyped Warcraft
Logs table entry per row, which is where the gear, pet and per-ability detail lives; one fight goes
from roughly 43 KB to 560 KB with it on. `report-encounter-casts` aggregates only the events one
`--limit` page returns, so it sets `casts.truncated` and a note when Warcraft Logs hands back a
`next_page_timestamp`.

Cast counts (`report-encounter-casts` and `ability-usage-summary`) count only `cast` events. The
Casts data type also returns `begincast` (a cast bar starting, including cancelled casts) and
`empowerstart`/`empowerend` (an empowered spell's charge); these are skipped, so `casts.event_count`
is the page size and `casts.cast_count` the counted casts. An empowered spell counts once per press,
a cast-time spell once per finished cast, and a channelled spell once when the channel starts.

Sampled cross-report analytics (many kills, one boss): `boss-kills`, `top-kills`,
`spec-kill-samples`, `kill-time-distribution`, `boss-spec-usage`, `comp-samples`,
`ability-usage-summary`.

## Output contract

Every command emits one JSON document with the shared envelope keys (`ok`, `provider`, `command`,
`kind`, `schema_version`, `query`, `provenance`, `data`, and `error` on failure) as defined in
[ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md).

Each command's payload is under `data`, once (`data.kills`, `data.rankings`, `data.fights`, ...);
nothing else is at the top level. `graphql`'s `data` is the GraphQL result's own `data` object,
`__schema` included under `--introspect`. Use `--fields` or `--compact` to bound large report
payloads.

Failures print the error envelope to stderr and exit with the shared codes: `1` generic, `2` usage
or invalid query, `3` auth, `4` not found, `5` network or upstream. A transport failure is always
an error envelope, never a traceback. A failure's `query` is the command's parsed parameters
(`{"reference": "abcd1234", "fight_id": 9999, ...}`), so the rejected input is machine-readable;
it can differ in shape from the success `query`. The `auth login` / `auth pkce-login` authorization
code and the global output flags are never echoed. `auth` subcommands are
labelled by their full path (`"command": "auth status"`) on success and failure alike.

Rejected input exits `2`: `missing_boss`, `missing_query`, `missing_scope`, `missing_spec`,
`invalid_query`, `invalid_variables`, `ambiguous_boss`, `boss_scope_mismatch`, and the OAuth
callback mismatches `missing_state`, `state_mismatch`, `redirect_uri_mismatch`. Auth problems exit
`3`, including `site_profile_mismatch` when the saved user token belongs to another `--site`.
Malformed upstream or local data exits `1`: `missing_talent_tree`, `invalid_response`,
`invalid_provider_payload`, `invalid_transport_packet`, `invalid_runtime_config`,
`missing_code_verifier`.

Partial GraphQL failures are surfaced, not swallowed: typed commands keep `data.graphql_warnings`
(every partial error from every request the command made) and add a note instead of pretending the
result is complete. Partial responses are never cached. `graphql` leaves `data` exactly as the
API returned it and puts the partial errors in `provenance.graphql_warnings`.

## Sampled analytics and trust

Sampled commands aggregate a bounded cohort of reports, never "all kills". Each one reports its
sample scope, exclusion and truncation counts, cache provenance, freshness, and citations, per
[SAFE_ANALYTICS_RULES.md](../foundation/SAFE_ANALYTICS_RULES.md).

`freshness.sampled_at` is when the command ran, not when the cohort was fetched; the cohort itself
can come from cache. `freshness.cache_hit_count`, `freshness.upstream_request_count`, and
`freshness.served_entirely_from_cache` make that visible.

`--zone-id` and `--boss-id`/`--boss-name` are validated against Warcraft Logs world data before the
sample is scanned, so a wrong id fails with `not_found` (exit 4) instead of returning `count: 0`.

When two raiders in one group each upload the pull, Warcraft Logs holds it as two reports. Those
are collapsed into one sampled kill: same guild id, encounter, difficulty and raid size, with
wall-clock start *and* end within 5 s of any report already folded into one of that guild's
pulls, so uploads a few seconds apart chain into one pull. Every open pull is a candidate,
so another pull that starts in between cannot split a double-logged one. Fights are clustered in
start order, so the result does not depend on report listing order, and the earliest-starting
report represents the pull. The collapse is reported, never silent —
`sample.duplicates_removed` counts it, every sampled command adds a note stating the rule, and the
kept kill's `duplicate_reports` cites the report codes and fight ids that were folded in.

Warcraft Logs has no cross-report pull id, and timing alone cannot tell two unrelated personal
logs apart, so a fight from a report with no guild is never collapsed; neither is a guild upload
merged with a personal upload of the same pull. Such a pull can therefore count twice. A fight
whose absolute window cannot be computed is always kept.

`ability-usage-summary` requests at most `--event-limit` cast events per sampled kill. Kills that
overflow that page are counted in `sample.kills_with_truncated_events_count`, and
`usage.total_casts_is_lower_bound` then marks every derived total as a floor.

`--spec-name` filters sampled kills by participant spec before aggregation; it does not turn the
query into a spec leaderboard. It takes the class too (`'Frost Mage'`), because a bare spec name
matches every class with that spec (see `SCOPING.md`); `boss-spec-usage` rows are keyed by class and
spec for the same reason. `spec-kill-samples` requires `--spec-name` and returns an explicit
participant cohort. For leaderboard questions use `encounter-rankings`.

## Talent transport

`report-player-talents` is deliberately narrow: one report, one fight, one actor. It needs fight
scope from `--fight-id` or a report URL with `#fight=<id>`, and it emits a packet only when every
selected `combatant_info.talentTree` row is fully formed; otherwise it fails with
`missing_talent_tree` rather than emitting a partial packet. `--out <path>` writes just the packet
JSON; `--allow-unlisted` permits an unlisted report.

Warcraft Logs performs pure structural validation only. It does not run SimulationCraft, so its
packets are `raw_only` with `validation.reason: simc_backend_unavailable` recorded. Run `simc` to add
validated `simc_split_talents`:

```bash
warcraftlogs report-player-talents <report> --fight-id <id> --actor-id <id> --out ./tmp/actor-packet.json
simc validate-talent-transport --build-packet ./tmp/actor-packet.json --out ./tmp/actor-packet-validated.json
warcraft talent-describe ./tmp/actor-packet-validated.json --apl-path <apl>
```

## Known gaps

- `character-rankings` carries a trust block plus the raw passthrough, but has been validated
  against a small set of public characters only.
- Encounter analytics stops at the typed player/cast/buff/aura/damage summaries; wave and phase
  segmentation is not implemented, so agents still derive those from raw timestamps.
- Cross-report analytics has no ranking-basis discovery beyond sampled fastest kills, and no
  kill-time-bounded cohort discovery.
- User auth is manual and per-command; the `warcraft` wrapper does not route user-scoped calls.
- Progress-race and saved-query surfaces are not exposed.

## Official endpoints

- OAuth docs: `https://www.warcraftlogs.com/api/docs`
- public GraphQL: `https://www.warcraftlogs.com/api/v2/client`
- user GraphQL: `https://www.warcraftlogs.com/api/v2/user`
- authorize: `https://www.warcraftlogs.com/oauth/authorize`
- token: `https://www.warcraftlogs.com/oauth/token`

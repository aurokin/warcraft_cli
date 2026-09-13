# Warcraft Logs CLI

`warcraftlogs` queries the official Warcraft Logs OAuth 2.0 + GraphQL API. It does not scrape.
It exposes typed commands for guilds, characters, rankings, reports, encounter analytics, and
sampled cross-report analytics, plus a raw `graphql` passthrough for queries no typed command covers.

Companion docs:
- [SCOPING.md](SCOPING.md) - scoping conventions and raw-GraphQL rules
- [PAYLOAD_KEYS.md](PAYLOAD_KEYS.md) - per-command payload keys and the deprecated legacy keys
- [CACHING.md](CACHING.md) - cache keys, TTLs, and derived-output trust fields
- [LIVE_MATRIX.md](LIVE_MATRIX.md) - live command matrix workflow
- [warcraftlogs-design-notes.md](../architecture/history/warcraftlogs-design-notes.md) - schema research and the original design record
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

Encounter analytics (one report, one fight): `report-encounter`, `report-encounter-players`,
`report-player-talents`, `report-encounter-casts`, `report-encounter-buffs`,
`report-encounter-aura-summary`, `report-encounter-aura-compare`,
`report-encounter-damage-source-summary`, `report-encounter-damage-target-summary`,
`report-encounter-damage-breakdown`.

Sampled cross-report analytics (many kills, one boss): `boss-kills`, `top-kills`,
`spec-kill-samples`, `kill-time-distribution`, `boss-spec-usage`, `comp-samples`,
`ability-usage-summary`.

## Output contract

Every command emits one JSON document with the shared envelope keys (`ok`, `provider`, `command`,
`kind`, `schema_version`, `query`, `provenance`, `data`, and `error` on failure) as defined in
[ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md).

The payload body stays at the top level next to those keys so existing agent field paths keep
working, and the per-command canonical key plus the older primary key are both still emitted and
listed in [PAYLOAD_KEYS.md](PAYLOAD_KEYS.md). `data` mirrors that body for every command, so
agents can read the envelope slot without provider-specific paths; the top-level copies are
deprecated. Use `--fields` or `--compact` to bound large report payloads.

Failures print the error envelope to stderr and exit with the shared codes: `1` generic, `2` usage
or invalid query, `3` auth, `4` not found, `5` network or upstream. A transport failure is always
an error envelope, never a traceback.

Partial GraphQL failures are surfaced, not swallowed: the payload keeps `graphql_warnings` and adds
a note instead of pretending the result is complete.

## Sampled analytics and trust

Sampled commands aggregate a bounded cohort of reports, never "all kills". Each one reports its
sample scope, exclusion and truncation counts, cache provenance, freshness, and citations, per
[SAFE_ANALYTICS_RULES.md](../foundation/SAFE_ANALYTICS_RULES.md).

`--spec-name` filters sampled kills by participant spec before aggregation; it does not turn the
query into a spec leaderboard. `spec-kill-samples` requires `--spec-name` and returns an explicit
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

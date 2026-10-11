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
endpoints need a saved user token from the authorization-code or PKCE flow. Typed reads default
to `--endpoint client`, even when a saved user token exists. Use `warcraftlogs --endpoint user
report <private-code>` for private data, or `--endpoint auto` to deliberately prefer a locally valid
saved user token. Rejected user requests never silently fall back to public data. Raw `graphql`
retains its command-local `--endpoint` option (default `auto`); `auth whoami` always uses user auth.

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
`current_user()` for user access). Pass `--no-live` for local readiness only. `doctor`'s `status` is
`ready` when the selected command endpoint is ready, and `degraded` otherwise.
`auth.command_endpoint_policy`, `auth.command_endpoint`, and `auth.command_access` distinguish
public readiness from saved user access. A rejected saved token does not block public commands;
`warcraftlogs --endpoint user doctor` reports its failure. Each capability names its access reason. `doctor` also reports
`installed`, `language`, and the resolved `cache` configuration (backend, directory, TTLs); a Redis
backend that does not answer, or a cache config that does not parse (`cache.error.code:
"invalid_cache_config"`), is `cache.available: false` with the reason in `cache.error`, and makes
`status` `degraded`.

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

A report code exists only on its own site. A report URL names the site by its host, so `resolve`
and `search` put that site's `--site` in the follow-up command, and the report commands fail with
`invalid_query` (exit 2) when the URL's site is not the selected `--site`.

Every report command (`report`, `report-fights`, `report-events`, the `report-encounter*` family and
the rest) takes a report URL or a bare report code. A URL's `#fight=N` or `?fight=N`, or a bare
`CODE#fight=N`, scopes the commands that take `--fight-id` when the flag is absent; an explicit
`--fight-id` wins. Localized hosts (`de.warcraftlogs.com`,
`ko.classic.warcraftlogs.com`) work; when the host names no site (`de.`), the selected `--site`
applies. An empty or malformed reference, or a URL whose host is not `warcraftlogs.com` or one of
its subdomains, is `invalid_query` (exit 2) before any request; `search` and
`resolve` reject an empty query the same way.

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
code and return a discovery hint for anything else. Free text is never sent to Warcraft Logs, so its
`total_matches: 0` counts report references in the query, not reports. The one matched row carries the report `url` and
`report_reference` (`code`, `fight_id`, `source_url`). A report URL resolves at `high` confidence. A bare
code is judged by its shape alone, so `resolve` answers it at `medium` confidence with `resolved: false`
and `next_command: null`; its command is still in `match.follow_up.command`.

`server`, `guild`, `guild-rankings`, `guild-members`, `guild-attendance`, `character` and
`character-rankings` take a realm in any spelling (`Azjol-Nerub`, `azjolnerub`, `Mal'Ganis`) and try
each slug spelling until Warcraft Logs finds the entity. `reports`/`guild-reports` and sampled
`--guild-realm` use the same fallback. `encounter-rankings --server-slug` sends one slug
(`Azjol-Nerub` becomes `azjol-nerub`), so pass Warcraft Logs' own slug there
(`azjolnerub`, as `server` reports it).
Most native-script realm names are their own Warcraft Logs slug (`아즈샤라`, `血之谷`, `Гордунни`), but
Warcraft Logs slugs some Russian realms in English (`Ревущий фьорд` is `howling-fjord`, as are
`Ясеневый лес`, `Борейская тундра`, `Черный Шрам` and `Разувий`). When no spelling of a non-Latin name
is found, those realm-taking commands read the region's server list (1-3 pages, cached like other
world data) and retry with the slug whose display name matches. The single-slug ranking flag does not:
pass `howling-fjord` there.

Regions are `us`, `eu`, `kr`, `tw` or `cn`, or an alias such as `na`; `oce`/`oceanic` read `us`,
where Warcraft Logs keeps Oceanic realms (subregion Oceanic). Any other region is `invalid_query`
(exit 2) before a request, not a `not_found`.

`zones --expansion-id` takes a Warcraft Logs expansion id (`warcraftlogs expansions`; Midnight is 7,
where Raider.IO numbers it 11). An id Warcraft Logs has no expansion for is `invalid_query` (exit 2)
naming the valid ids, instead of an empty zone list.

Warcraft Logs answers a filter it does not recognise unfiltered rather than rejecting it, so the
CLI rejects one locally with `invalid_query` (exit 2): an unknown `encounter-rankings --class-name`,
an unknown `character-rankings --spec-name` on the retail site, and a `--guild-name`,
`--guild-region` or `--guild-realm` given without the other two on `reports` and the sampled
commands. The spec list is retail's, so `--site classic` and `--site fresh` pass a spec through
(Combat exists there). `--class-name` and `--spec-name` read any provider's spelling and the shared
shorthand (`Death Knight`, `death-knight`, `dk`; `Beast Mastery`, `hunter-beastmastery`, `bm`,
`bm hunter`) and send Warcraft Logs' own `DeathKnight`/`BeastMastery`. Warcraft Logs needs a class
with a spec on `encounter-rankings`, so a spec spelling that names one class (`bm hunter`, `fdk`,
`ret`) supplies it when `--class-name` is absent; a bare `frost` still needs `--class-name`, and a
`--spec-name` of another class than `--class-name` is `invalid_query` (exit 2). Float flags (`--ability-id`,
`--kill-time-min` and the rest) reject `nan` and `inf` as `invalid_argument` (exit 2).

`--difficulty` takes an id or a retail name: `lfr` = 1, `normal` = 3, `heroic` = 4, `mythic` = 5
(`warcraftlogs zone <id>` lists a zone's difficulties). The GraphQL enum flags (`--hostility-type`,
`--kill-type`, `--view-by`, `--data-type`, `--compare`, `--timeframe`, `--leaderboard`,
`--hard-mode-level`) list their values in `--help`, ignore case and hyphens (`damage-done`), and
reject any other value as `invalid_argument` (exit 2) with the valid values, before a request.
`--start-time`/`--end-time` mean two things: on `reports`, `guild-reports` and the sampled commands
they bound the report list and take UNIX epoch milliseconds or an ISO-8601 date (`2026-09-01` or
`20260901`, UTC unless it carries an offset); on `report-events`, `report-table`, `report-graph` and
`report-player-details` they are milliseconds from the report's start.

The row cap is `--limit` everywhere: `encounter-rankings`, `character-rankings` and the sampled
commands still take `--top`, which shipped first, and `report-encounter-casts` names its event page
`--event-limit` (as `ability-usage-summary` does), still taking `--limit`.

Raid rankings rank healers on healing unless told otherwise; when no metric is sent Warcraft Logs
ranks every role on dps in a raid and on score in Mythic+. Without `--metric`, `encounter-rankings`
sends `playerscore` in a Mythic+ zone and, in a raid zone, `hps` for a healer `--spec-name`
(Discipline, Holy, Mistweaver, Preservation, Restoration) and `dps` otherwise; `query.metric` names
the metric that ranked the rows. `report-rankings` without `--player-metric` sends Warcraft Logs'
`default` (dps for raid fights, score for Mythic+ runs) and, when a ranked fight is a raid fight,
ranks those fights' healers on `hps` (a second request); `query.player_metric` and
`query.healer_metric` echo both. A `--player-metric` applies to every role in one request.

Auth: `auth status`, `auth client`, `auth token`, `auth login`, `auth pkce-login`, `auth whoami`,
`auth logout`.

World and static metadata: `regions`, `expansions`, `server`, `zones`, `zone`, `encounter`.

Guilds: `guild`, `guild-members`, `guild-attendance`, `guild-rankings`, `guild-reports`.

Characters: `character`, `character-rankings`.

`guild-members`, `character` and `character-rankings` return `class_id` in Warcraft Logs' own
numbering (`gameData.classes`, the same on every site: 1 Death Knight, 2 Druid, 3 Hunter, 4 Mage,
5 Monk, 6 Paladin, 7 Priest, 8 Rogue, 9 Shaman, 10 Warlock, 11 Warrior, 12 Demon Hunter, 13 Evoker),
not Blizzard's, so each also carries `class_name`.

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
Warcraft Logs has always returns a roster. A `--start-time` after `--end-time`, or one after every
selected fight ended, is `invalid_query` (exit 2) on `report-events`, `report-table`,
`report-graph`, and `report-player-details`.

Encounter analytics (one report, one fight): `report-encounter`, `report-encounter-players`,
`report-player-talents`, `report-encounter-casts`, `report-encounter-buffs`,
`report-encounter-aura-summary`, `report-encounter-aura-compare`,
`report-encounter-damage-source-summary`, `report-encounter-damage-target-summary`,
`report-encounter-damage-breakdown`.

The aura and damage summaries emit typed rows only. `--include-raw` attaches the untyped Warcraft
Logs table entry per row, which is where the gear, pet and per-ability detail lives; one fight goes
from roughly 43 KB to 560 KB with it on. `report-encounter-casts` aggregates only the events one
`--event-limit` page returns, so it sets `casts.truncated` and a note when Warcraft Logs hands back a
`next_page_timestamp`. `report-events` returns one raw page too: when `next_page_timestamp` is set,
a note says to fetch the next page with the same filters plus `--start-time <next_page_timestamp>`.
Warcraft Logs returns no events for a start time without an end, so a fight-scoped request with
`--start-time` and no `--end-time` runs to the end of the selected fights, echoed as `query.end_time`.

Buff and aura rows name their actor by role. In a Warcraft Logs Buffs table, `--view-by source`
(the default) groups rows by the actor that had the aura and `--view-by target` by the actor that
applied it, so the rows carry `aura_holder` or `applied_by`, never `source`/`target`, and
`buffs.row_actor`, `aura_summary.row_actor` and `comparison.row_actor` say which. `--source-id`
pins the grouping actor, and Warcraft Logs then groups by the other one (its table says
`useTargets: true`): under the default view `--source-id 18` returns `applied_by` rows, the casters
of the aura actor 18 had. With `--ability-id`, each row's `aura` is the requested ability.

Encounter windows (`--window-start-ms`/`--window-end-ms` and the compare windows) are clamped to the
fight. The `query` block carries `effective_window_start_ms`, `effective_window_end_ms`,
`effective_window_duration_ms` and `window_clamped`, and a note names any window that ran past the
fight, so uptime is read against the span that was really covered.

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
`invalid_query` (including a flag value Warcraft Logs' GraphQL schema rejects, such as
`--metric nope`), `invalid_argument` (a flag value the CLI rejects itself, such as `--data-type nope`
or `--difficulty mythc`), `invalid_variables`, `ambiguous_boss`, `boss_scope_mismatch`, and the OAuth
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
are collapsed into one sampled kill when they share encounter, difficulty, raid size and keystone
level and either come from the same guild id with wall-clock start *and* end within 5 s, or list
the same set of players with start and end within 30 s. Available player rosters must agree even
for the same-guild shortcut; contradictory rosters keep the pulls separate. Player details are
fetched only for candidates and reused for spec filtering. Timing is checked against every report already folded into a pull, so uploads a few
seconds apart chain into one pull. Every open pull is a candidate,
so another pull that starts in between cannot split a double-logged one. Fights are clustered in
start order, so the result does not depend on report listing order, and the earliest-starting
report represents the pull. The collapse is reported, never silent —
`sample.duplicates_removed` counts it, every sampled command adds a note stating the rule, and the
kept kill's `duplicate_reports` cites the report codes and fight ids that were folded in.

When no kill matches (`sample.matched_boss_kill_count: 0`), every sampled command adds a note naming
how many reports and fights were scanned and how to widen the cohort: raise `--report-pages`, narrow
`--start-time`/`--end-time`, or use `encounter-rankings`, which ranks every logged kill, for a late or
rarely killed boss. An empty cohort is not evidence about the boss.

Warcraft Logs has no cross-report pull id, and timing alone cannot tell two unrelated personal
logs apart, so a fight from a report with no guild is collapsed only on a matching roster. When
two fights fall within 5 s of each other, either one has no guild and their rosters differ, the
later-starting one is kept, marked `possible_duplicate_of` the earlier pull, counted in
`sample.possible_duplicates`, and named in a note. A report that the listing returns on two pages
is counted once. A fight whose absolute window cannot be computed is always kept.

`sample.difficulty_counts` gives kills per difficulty and `sample.keystone_level_counts` runs per
Mythic+ key level. A note warns when a cohort mixes difficulties (no `--difficulty`) or key levels:
those kill times are not comparable, and the fastest-kill order ranks raw durations across them.
Mythic+ fight rows carry `keystone_level` and `keystone_time_ms`, the in-game key timer, which
differs from `end_time - start_time` by a few seconds.

`ability-usage-summary` requests at most `--event-limit` cast events per sampled kill. Kills that
overflow that page are counted in `sample.kills_with_truncated_events_count`, and
`usage.total_casts_is_lower_bound` then marks every derived total as a floor. It counts
player-side casts only, so a boss ability reads zero on every kill; use `report-encounter-casts
--hostility-type enemies` for boss casts.

Sampled commands scan reports that are still being logged as well as finished ones: a kill fight is
final once it ends, and the listing puts the most recently updated reports first. Each kill row says
`report_finished`, and `sample.live_report_count` / `sample.finished_report_count` split the listing.

`--spec-name` filters sampled kills by participant spec before aggregation; it does not turn the
query into a spec leaderboard. It takes the class too (`'Frost Mage'`, `deathknight-frost`, `fdk`), because a bare spec name
matches every class with that spec (see `SCOPING.md`). On the retail site a name that is no spec,
or a class with no such spec (`'Frost Rogue'`), is `invalid_query` (exit 2) instead of an empty
cohort; `boss-spec-usage` rows are keyed by class and
spec for the same reason. `spec-kill-samples` requires `--spec-name` and returns an explicit
participant cohort. For leaderboard questions use `encounter-rankings`.

## Talent transport

`report-player-talents` is deliberately narrow: one report, one fight, one actor. It needs fight
scope from `--fight-id` or a report URL with `#fight=<id>`, and it emits a packet only when every
selected `combatant_info.talentTree` row is fully formed; otherwise it fails with
`missing_talent_tree` rather than emitting a partial packet. `--out <path>` writes just the packet
JSON; `--allow-unlisted` permits an unlisted report.

Warcraft Logs performs pure structural validation only. It does not run SimulationCraft, so its
packets are `raw_only` with `validation.reason: simc_backend_unavailable` recorded, or
`missing_class_spec_identity` / `unsupported_actor_class` when the actor's class and spec do not
resolve. Run `simc` to add
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

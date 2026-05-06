## When To Use

Use `warcraftlogs` when the user needs official Warcraft Logs API data instead of a guide or ranking-site summary.

Best fits:
- guild progression from the official source
- character identity lookups on the log platform
- report and fight inspection by report code
- world metadata like regions, servers, zones, and encounters

## Start Here

- health/auth:
  - `warcraftlogs doctor`
  - `warcraftlogs auth status`
  - `warcraftlogs auth client`
  - `warcraftlogs auth token`
  - `warcraftlogs auth whoami`
  - `warcraftlogs auth login --redirect-uri <uri>`
  - `warcraftlogs auth pkce-login --redirect-uri <uri>`
  - `warcraftlogs auth logout`
  - `warcraftlogs rate-limit`
- world metadata:
  - `warcraftlogs regions`
  - `warcraftlogs expansions`
  - `warcraftlogs server <region> <slug>`
  - `warcraftlogs zones`
  - `warcraftlogs zone <id>`
  - `warcraftlogs encounter <id>`
- direct lookup:
  - `warcraftlogs guild <region> <realm> <name>`
  - `warcraftlogs guild-members <region> <realm> <name>`
  - `warcraftlogs guild-attendance <region> <realm> <name>`
  - `warcraftlogs guild-rankings <region> <realm> <name>`
  - `warcraftlogs guild-reports <region> <realm> <name>`
  - `warcraftlogs character <region> <realm> <name>`
  - `warcraftlogs character-rankings <region> <realm> <name>`
  - `warcraftlogs encounter-rankings --zone-id ... --boss-id ...`
  - `warcraftlogs reports --guild-region ... --guild-realm ... --guild-name ...`
  - `warcraftlogs report <code>`
  - `warcraftlogs report-fights <code>`
  - `warcraftlogs report-player-details <code> --fight-id ...`
  - `warcraftlogs report-player-talents <report-url-or-code> --fight-id ... --actor-id ...`
  - `warcraftlogs report-master-data <code>`
  - `warcraftlogs report-events <code> --fight-id ...`
  - `warcraftlogs report-table <code> --data-type damage-done --fight-id ...`
  - `warcraftlogs report-graph <code> --data-type damage-done --fight-id ...`
  - `warcraftlogs report-rankings <code> --fight-id ... --player-metric dps`
  - `warcraftlogs report-encounter <report-url-or-code>`
  - `warcraftlogs report-encounter-players <report-url-or-code>`
  - `warcraftlogs report-encounter-casts <report-url-or-code>`
  - `warcraftlogs report-encounter-buffs <report-url-or-code>`
  - `warcraftlogs report-encounter-aura-summary <report-url-or-code> --ability-id ...`
  - `warcraftlogs report-encounter-aura-compare <report-url-or-code> --ability-id ... --left-window-start-ms ... --left-window-end-ms ... --right-window-start-ms ... --right-window-end-ms ...`
  - `warcraftlogs report-encounter-damage-source-summary <report-url-or-code>`
  - `warcraftlogs report-encounter-damage-target-summary <report-url-or-code>`
  - `warcraftlogs report-encounter-damage-breakdown <report-url-or-code>`
  - `warcraftlogs boss-kills --zone-id ... --boss-id ... --difficulty ...`
  - `warcraftlogs top-kills --zone-id ... --boss-id ... --difficulty ...`
  - `warcraftlogs kill-time-distribution --zone-id ... --boss-id ... --difficulty ...`
  - `warcraftlogs boss-spec-usage --zone-id ... --boss-id ... --difficulty ...`
  - `warcraftlogs comp-samples --zone-id ... --boss-id ... --difficulty ...`
  - `warcraftlogs ability-usage-summary --zone-id ... --boss-id ... --difficulty ... --ability-id ...`

## Current Boundaries

- current support is retail/main site only
- public OAuth client credentials are the default auth mode
- manual user-auth groundwork now exists for authorization-code and PKCE exchange, plus saved user-token verification via `warcraftlogs auth whoami`
- current surface works both standalone and through the root `warcraft` wrapper, but wrapper discovery is still intentionally narrow
- current commands use typed payloads, not raw GraphQL passthrough

## Inputs

- credentials are loaded in this order:
  - repo-local `.env.local`
  - XDG config: `~/.config/warcraft/providers/warcraftlogs.env`
  - process environment
- runtime auth state is stored separately:
  - `~/.local/state/warcraft/providers/warcraftlogs.json`
- required variables:
  - `WARCRAFTLOGS_CLIENT_ID`
  - `WARCRAFTLOGS_CLIENT_SECRET`
- region/realm inputs still benefit from normalized forms:
  - `us`
  - `illidan`

## Good Consumer Workflows

- guild snapshot:
  - `warcraftlogs guild us illidan Liquid`
- guild progress in a specific zone:
  - `warcraftlogs guild us illidan Liquid --zone-id 38`
- guild rankings in a specific zone:
  - `warcraftlogs guild-rankings us illidan Liquid --zone-id 38 --size 20 --difficulty 5`
- guild roster:
  - `warcraftlogs guild-members us illidan Liquid --limit 5`
- guild attendance history:
  - `warcraftlogs guild-attendance us illidan Liquid --limit 2`
- guild report history:
  - `warcraftlogs guild-reports us illidan Liquid --limit 10`
- character identity:
  - `warcraftlogs character us illidan Roguecane`
- character rankings, when the API allows them:
  - `warcraftlogs character-rankings us illidan Roguecane --zone-id 38 --difficulty 5 --metric dps --size 20`
- encounter rankings for real boss/class/spec leaderboard queries:
  - `warcraftlogs encounter-rankings --zone-id 46 --boss-id 3180 --difficulty 5 --class-name Druid --spec-name Balance --metric dps --top 10`
- guild report listing:
  - `warcraftlogs reports --guild-region us --guild-realm illidan --guild-name Liquid --limit 10`
- report inspection:
  - `warcraftlogs report <code>`
  - `warcraftlogs report-fights <code> --difficulty 5`
  - `warcraftlogs report-encounter 'https://www.warcraftlogs.com/reports/<code>#fight=47'`
  - `warcraftlogs report-encounter-players 'https://www.warcraftlogs.com/reports/<code>#fight=47'`
  - `warcraftlogs report-encounter-casts 'https://www.warcraftlogs.com/reports/<code>#fight=47' --preview-limit 20`
  - `warcraftlogs report-encounter-buffs 'https://www.warcraftlogs.com/reports/<code>#fight=47' --view-by source`
  - `warcraftlogs report-encounter-aura-summary 'https://www.warcraftlogs.com/reports/<code>#fight=47' --ability-id 20473 --window-start-ms 30000 --window-end-ms 90000`
  - `warcraftlogs report-encounter-aura-compare 'https://www.warcraftlogs.com/reports/<code>#fight=47' --ability-id 20473 --left-window-start-ms 30000 --left-window-end-ms 90000 --right-window-start-ms 90000 --right-window-end-ms 150000`
  - `warcraftlogs report-encounter-damage-source-summary 'https://www.warcraftlogs.com/reports/<code>#fight=47' --window-start-ms 30000 --window-end-ms 90000`
  - `warcraftlogs report-encounter-damage-target-summary 'https://www.warcraftlogs.com/reports/<code>#fight=47' --window-start-ms 30000 --window-end-ms 90000`
  - `warcraftlogs report-encounter-damage-breakdown 'https://www.warcraftlogs.com/reports/<code>#fight=47' --window-start-ms 30000 --window-end-ms 90000`
  - `warcraftlogs report-player-details <code> --fight-id 47`
  - `warcraftlogs report-player-talents <code> --fight-id 47 --actor-id 1739`
  - `warcraftlogs report-master-data <code> --actor-type Player`
  - `warcraftlogs report-events <code> --fight-id 47 --limit 100`
  - `warcraftlogs report-table <code> --data-type damage-done --fight-id 47`
  - `warcraftlogs report-graph <code> --data-type damage-done --fight-id 47`
  - `warcraftlogs report-rankings <code> --fight-id 47 --player-metric dps --timeframe historical --compare rankings`
- sampled cross-report analytics:
  - `warcraftlogs boss-kills --zone-id 38 --boss-id 3012 --difficulty 5 --top 10`
  - `warcraftlogs top-kills --zone-id 38 --boss-name Dimensius --difficulty 5 --top 5`
  - `warcraftlogs kill-time-distribution --zone-id 38 --boss-id 3012 --difficulty 5 --bucket-seconds 30`
  - `warcraftlogs boss-spec-usage --zone-id 38 --boss-id 3012 --difficulty 5 --top 10`
  - `warcraftlogs comp-samples --zone-id 38 --boss-id 3012 --difficulty 5 --top 5`
  - `warcraftlogs ability-usage-summary --zone-id 38 --boss-id 3012 --difficulty 5 --ability-id 20473 --preview-limit 5`

## Notes

- prefer `warcraftlogs` when official log data matters more than convenience summaries
- use `wowprogress` or `raiderio` for their own ranking/profile strengths, not as substitutes for Warcraft Logs report data
- `character-rankings` can return a provider permission error or a provider-side failure for some characters; treat it as useful but less stable than `guild-rankings`
- `guild-members` depends on Warcraft Logs being able to verify the guild roster for that game; treat it as a retail-capable roster surface, not a universal promise across every future site profile
- `guild-attendance` is part of the official schema, but live public queries can still fail with a provider-side internal error; use it when it works, but do not assume the endpoint is fully stable
- `guild-reports` is the easiest official path when the user wants report history for one guild without manually shaping the broader `reports` query
- for one-fight analysis from a report link, prefer `report-encounter*` commands over manually combining `report-fights`, `report-player-details`, and `report-events`
- `report-encounter-casts`, `report-encounter-buffs`, and `report-encounter-damage-breakdown` support encounter-relative timeline filters:
  - `--window-start-ms`
  - `--window-end-ms`
- `report-encounter-casts` also includes additive `by_target` and `by_source_target` summaries for target-scoped cast analysis inside the selected fight/window
- `report-encounter-aura-summary` is the narrower aura workflow: it requires one explicit `--ability-id` and returns typed source rows with preserved reported buff-table fields for that selected fight/window
- `report-encounter-aura-compare` is stricter still: same report, same fight, same aura, and two fully explicit windows; use it when you want typed per-source deltas without pretending two different pulls or scopes are directly comparable
- `report-encounter-damage-source-summary` is the equivalent narrow damage workflow for source-grouped damage rows; use it when you want typed source identities without depending on the broader raw breakdown payload alone
- `report-encounter-damage-target-summary` is the target-grouped sibling; use it when the question is really about damage on explicit encounter targets or adds
- those encounter-scoped commands surface the resolved absolute `start_time` and `end_time` in the payload so the agent does not have to derive report timestamps manually
- `report-player-details` is the easiest way to inspect the participants in a report slice before deeper event/table work
- `report-player-talents` is the first narrow build-transport lane:
  - use it when you need one actor's selected talents for one explicit fight
  - it returns a scoped `talent_transport_packet` sourced from `combatant_info.talentTree`
  - for normal multi-fight reports, give it `--fight-id` or a report URL that already includes `#fight=<id>`
  - it only emits a packet when every selected talent-tree row is fully formed, and then keeps normalized raw `entry/node_id/rank` rows from the source tree as evidence
  - when local SimulationCraft trait data resolves every entry and the reconstructed build round-trips, it also includes validated `simc_split_talents`
  - otherwise it stays `raw_only` and tells you why validation could not be proven
  - malformed or incomplete talent-tree rows fail with `missing_talent_tree` instead of emitting a partial packet
- `report-fights` is still the stable broad fight-list surface; use it to get fight IDs first, then move to `report-player-details`, `report-events`, `report-table`, or `report-graph` for deeper filtered analysis
- `report-table` and `report-graph` accept user-friendly enum filters like `damage-done` and normalize them for the API
- `report-events` intentionally requires a narrowed slice such as `--fight-id`, `--encounter-id`, `--start-time`, or `--end-time`
- `report-events` can still return `events: null` for some valid report slices; use it as a typed event-query surface, not a guarantee of non-empty data
- `report-rankings` can legitimately return zero rows for a valid public report slice
- `encounter-rankings` is the ranking surface to use when the user means boss/class/spec leaderboard results like "top Balance parses on Vanguard"
- `boss-kills`, `top-kills`, and `kill-time-distribution` are sampled cross-report analytics, not a promise that the CLI searched every possible public report
- `boss-kills` and `top-kills` do accept `--spec-name`, but on those sampled commands the filter means "keep sampled kills whose participants included that spec", not "return spec rankings"
- `boss-spec-usage` is also sampled cross-report analytics; it reports spec presence within the filtered finished-kill cohort, not a site-wide meta snapshot
- `comp-samples` is sampled cross-report analytics too; it returns sampled kill rosters plus additive class-presence and exact class-signature summaries for that filtered cohort
- `ability-usage-summary` is sampled cross-report analytics too; it reports explicit cast counts for one requested `--ability-id` across the filtered finished-kill cohort
- these sampled analytics commands now include freshness and citation metadata for the sampled report cohort so agents can preserve trust boundaries when composing follow-up steps
- those sampled analytics intentionally skip unfinished live reports and surface sample/truncation metadata instead of faking global certainty
- `warcraftlogs auth status` is the first place to check when auth looks wrong; it shows credential source and whether any persisted auth state exists
- `warcraftlogs auth login --redirect-uri ...` and `warcraftlogs auth pkce-login --redirect-uri ...` are two-step flows:
  - first run prints the authorize URL and saves pending state locally
  - second run exchanges the returned `code` and `state`
- `warcraftlogs auth whoami` is the clearest verification that a saved user token actually works against the private user endpoint
- always pass `--scope view-user-profile` when running `auth login` or `auth pkce-login`; without it the saved token can authenticate but cannot read private/guild reports or `userData.currentUser`, and report queries fall back to anonymous client-credentials access
- once a saved user token exists, every `warcraftlogs` command routes its GraphQL through `/api/v2/user` automatically, so private-report visibility is gated entirely on the saved token's scopes — verify with `warcraftlogs auth status` or `warcraftlogs auth token` (look for `scopes.has_view_user_profile`)
- the token-exchange step of `auth login` and `auth pkce-login` reports `scopes.granted`, `scopes.requested`, `scopes.has_view_user_profile`, and a `scope_warning` so a missing `view-user-profile` is visible immediately

# Warcraft Logs Scoping

Use this page when a Warcraft Logs command needs an explicit report, fight, encounter, actor, ability, or sampling boundary.

## Report References

Encounter-scoped commands accept either a bare report code or a report URL:

```bash
warcraftlogs report-encounter 7Rc3HPCWGYy1z4tT --fight-id 25
warcraftlogs report-encounter 'https://www.warcraftlogs.com/reports/7Rc3HPCWGYy1z4tT#fight=25'
```

The URL may carry the fight as `?fight=25` or `#fight=25`. If both a URL fight and `--fight-id`
are provided, `--fight-id` is the explicit override.
Report codes are 16 letters and digits and need not contain a digit (`JVFTxcKCqrvpaAzD`); `search`
and `resolve` recognise such a code bare or inside a `/reports/<code>` URL, but not a CamelCase
word such as `HavocDemonHunter`.
A trash fight (Warcraft Logs encounter ID 0) is sliced by its fight ID alone, with no encounter or
kill-type filter.

## Encounter And Window Scope

Use these flags to narrow report, encounter, table, graph, ranking, and sampled analytics queries:

- `--fight-id`: one report fight; repeat where the command supports multiple fights
- `--encounter-id`: one Warcraft Logs encounter id

A `--fight-id`, `--encounter-id`, or `--difficulty` that matches no fight in the report fails with
`not_found` (exit 4) on `report-events`, `report-table`, `report-graph`, `report-rankings`, and
`report-player-details`, with the rejected slice echoed in the failure envelope's `query`.
- `--difficulty`: Warcraft Logs difficulty id or retail name (`lfr` = 1, `normal` = 3, `heroic` = 4, `mythic` = 5)
- `--zone-id`: provider zone id
- `--start-time` / `--end-time`: on report slices (`report-events`, `report-table`, `report-graph`, `report-player-details`), milliseconds from the report start; on `reports`, `guild-reports` and the sampled commands, UNIX epoch milliseconds or an ISO-8601 date (UTC)
- `--window-start-ms` / `--window-end-ms`: encounter-relative timestamps on supported `report-encounter*` commands; a window that starts at or after the fight's end fails with `invalid_query` (exit 2) instead of answering zero, and one that ends past the fight is clamped to it (`query.effective_window_*`, `query.window_clamped` and a note)
- `--left-window-start-ms` / `--left-window-end-ms` and `--right-window-start-ms` / `--right-window-end-ms`: explicit comparison windows for `report-encounter-aura-compare`
- `--boss-id` / `--boss-name`: sampled cross-report boss scope where supported

## Identity Scope

Use identity flags when the question is about one actor, target, ability, event family, or table grouping:

- `--source-id`: source actor id; on Buffs tables it pins the grouping actor (the aura holder under `--view-by source`, the caster under `--view-by target`) and the rows then name the other one
- `--target-id`: target actor id; on Buffs tables it filters the actor the rows do not group by. Buffs event reads use it to select the aura caster, identified by `sourceID` in the raw events
- `--ability-id`: ability game id
- `--hostility-type`: `Friendlies` or `Enemies`
- `--kill-type`: `All`, `Encounters`, `Kills`, `Trash` or `Wipes`
- `--data-type`: event/table/graph data type, for example `casts` or `damage-done` (`--help` lists them all)
- `--view-by`: table/graph grouping: `Default`, `Ability`, `Source` or `Target`

These enum flags ignore case and hyphens and reject any other value before a request, listing the valid ones.
- `--wipe-cutoff`: provider wipe cutoff where supported

Returned actors, abilities, encounters, and talent packets use the shared identity contract documented in [IDENTITY_CONTRACT.md](../foundation/IDENTITY_CONTRACT.md). Preserve those identity objects when chaining commands; do not re-resolve names if the payload already includes a stable id.

## Cross-Report Sampling Scope

Sampled analytics commands such as `boss-kills`, `top-kills`, `spec-kill-samples`, `kill-time-distribution`, `boss-spec-usage`, `comp-samples`, and `ability-usage-summary` operate on bounded report cohorts. Their scope flags are part of the trust contract:

- guild filters: `--guild-region`, `--guild-realm`, `--guild-name`
- report budget: `--report-pages`, `--reports-per-page`
- time filters: `--start-time`, `--end-time`
- encounter filters: `--zone-id`, `--boss-id`, `--boss-name`, `--difficulty`
- participant filter: `--spec-name` keeps sampled kills that include that spec; it is not a spec leaderboard. Spec names repeat across classes, so pass the class too (`'Frost Mage'`, `frost-death-knight`); a bare spec name matches every class with that spec, lists them in `sample.matched_spec_classes`, and adds a note when there are several

`spec-kill-samples` requires `--spec-name` (alongside boss scope): it returns the participant filter as an explicit, labeled cohort (`cohort: spec_filtered_participant_kill_cohort`) rather than as an optional refinement of `boss-kills`.

One real pull that two raiders both uploaded is collapsed into a single sampled kill (same
encounter, difficulty and raid size, and either the same guild with wall-clock start and end
within 5 s, or the same players within 30 s).
`sample.duplicates_removed` counts the collapse and the kept kill's `duplicate_reports` cites the
folded-in report codes and fight ids. A kill timed like an earlier one whose roster differs, where
either report has no guild, is kept and marked `possible_duplicate_of`; `sample.possible_duplicates`
counts those.
`sample.difficulty_counts` and `sample.keystone_level_counts` show when a cohort mixes difficulties
or Mythic+ key levels, and a note says their kill times are not comparable.

Keep sample size, exclusions, truncation, deduplication, freshness, and citations with any
downstream analysis.

## Raw GraphQL

`warcraftlogs graphql` is the escape hatch for official API queries that are not yet covered by a typed command. It still uses the same auth, endpoint routing, partial-error, and JSON envelope behavior as typed commands.

```bash
warcraftlogs graphql \
  --query 'query Report($code: String!) { reportData { report(code: $code) { code title } } }' \
  --report-code 7Rc3HPCWGYy1z4tT

warcraftlogs graphql \
  --query @./query.graphql \
  --variables-json '{"code":"7Rc3HPCWGYy1z4tT"}' \
  --operation-name Report

cat ./query.graphql | warcraftlogs graphql --query - --var code=7Rc3HPCWGYy1z4tT
```

### Query Input

- `--query '<operation text>'`: literal GraphQL
- `--query @path/to/query.graphql`: read a file
- `--query -`: read stdin
- `--introspect`: run the built-in introspection query and return its result (`data.__schema`)

### Variables

- `--variables-json` must be a JSON object
- `--var key=value` is repeatable and JSON-coerces values when possible
- when the same key appears in both places, `--var` wins
- explicit variables always win over scoping helpers

### Scoping Helpers

Helper flags inject variables only when the query declares the matching variable:

| Flag | Injected variable |
| --- | --- |
| `--report-code` | `code` |
| `--fight-id` | `fightID` or `fightIDs` |
| `--encounter-id` | `encounterID` |
| `--start-time` | `startTime` |
| `--end-time` | `endTime` |
| `--difficulty` | `difficulty` |
| `--zone-id` | `zoneID` |
| `--source-id` | `sourceID` |
| `--target-id` | `targetID` |
| `--ability-id` | `abilityID` |
| `--allow-unlisted` | `allowUnlisted=true` |

If a query declares `$fightIDs: [Int]`, repeated `--fight-id` values are injected as a list. If it declares `$fightID: Int`, the first `--fight-id` value is injected as a scalar.

### Endpoint And Cache

- `--endpoint auto`: use the saved user token when one exists, otherwise client credentials
- `--endpoint client`: force `/api/v2/client`
- `--endpoint user`: force `/api/v2/user`
- `--cache-ttl 0`: default, no cache
- `--cache-ttl <seconds>`: opt in to cache for client-endpoint queries; cache identity includes endpoint, operation name, query text, and variables

User-endpoint raw queries are not cached, even when `--cache-ttl` is set, because saved user auth can switch accounts and may expose private report or `currentUser` data.

Partial GraphQL errors with useful data are emitted as `provenance.graphql_warnings`; `data` stays the GraphQL result verbatim, so an alias such as `notes` is never overwritten.

## Bounded complete event evidence

The default `report-events` remains one page. Use `--all-pages` to follow continuation timestamps
for exactly one fight or one explicit start/end window; `--data-type` is required. For example:

```bash
warcraftlogs --endpoint client --refresh report-events <code> --fight-id 1 --data-type casts \
  --all-pages --max-pages 20 --max-events 100000 --out events.jsonl --artifact-format jsonl
```

`--limit` controls each page (capped by the remaining event budget); `--max-pages` bounds event
page requests, with at most one additional fight validation request. Retries and the OAuth token
exchange are outside that logical GraphQL request count. API-point cost varies by query;
`export.bounds.api_point_budget: null` makes clear that this is not an exact point budget.

Read `export.complete` before counting the whole scope. It means provider pagination exhausted,
not a transactional or revision-validated snapshot; absent revisions are explicitly unknown. Bounds, missing data, partial GraphQL
errors, or changed report revisions produce an incomplete result with a stop reason.
Missing pagination cursors or nonfinite, boolean, or nonadvancing cursors fail as invalid responses,
including when an event bound would otherwise stop the page early.
`export.continuation.start_time` retains the next provider cursor. An unexpected oversized page
also records `skip_events`: re-fetch that original page and skip that many retained rows; do not
resume from the later provider cursor or events would be lost. Revision changes require restarting.

Artifacts preserve raw events, the selected filters/site/report, citations and freshness. JSON
stores one payload; JSONL starts with an `artifact` metadata record followed by `event` records.
An existing output file is never overwritten. No artifact is a transactional upstream snapshot:
cached pages can be older, and WCL can change JSON event semantics independently of report revision.

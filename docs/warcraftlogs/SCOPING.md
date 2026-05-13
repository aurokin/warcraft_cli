# Warcraft Logs Scoping

Use this page when a Warcraft Logs command needs an explicit report, fight, encounter, actor, ability, or sampling boundary.

## Report References

Encounter-scoped commands accept either a bare report code or a report URL:

```bash
warcraftlogs report-encounter 7Rc3HPCWGYy1z4tT --fight-id 25
warcraftlogs report-encounter 'https://www.warcraftlogs.com/reports/7Rc3HPCWGYy1z4tT#fight=25'
```

If both a URL fragment and `--fight-id` are provided, `--fight-id` is the explicit override.

## Encounter And Window Scope

Use these flags to narrow report, encounter, table, graph, ranking, and sampled analytics queries:

- `--fight-id`: one report fight; repeat where the command supports multiple fights
- `--encounter-id`: one Warcraft Logs encounter id
- `--difficulty`: provider difficulty id
- `--zone-id`: provider zone id
- `--start-time` / `--end-time`: absolute report timestamps in milliseconds
- `--window-start-ms` / `--window-end-ms`: encounter-relative timestamps on supported `report-encounter*` commands
- `--left-window-start-ms` / `--left-window-end-ms` and `--right-window-start-ms` / `--right-window-end-ms`: explicit comparison windows for `report-encounter-aura-compare`
- `--boss-id` / `--boss-name`: sampled cross-report boss scope where supported

## Identity Scope

Use identity flags when the question is about one actor, target, ability, event family, or table grouping:

- `--source-id`: source actor id
- `--target-id`: target actor id
- `--ability-id`: ability game id
- `--hostility-type`: provider hostility enum
- `--kill-type`: provider kill enum
- `--data-type`: event/table/graph data type, for example `casts` or `damage-done`
- `--view-by`: table/graph grouping, for example `source` or `target`
- `--wipe-cutoff`: provider wipe cutoff where supported

Returned actors, abilities, encounters, and talent packets use the shared identity contract documented in [IDENTITY_CONTRACT.md](../foundation/IDENTITY_CONTRACT.md). Preserve those identity objects when chaining commands; do not re-resolve names if the payload already includes a stable id.

## Cross-Report Sampling Scope

Sampled analytics commands such as `boss-kills`, `top-kills`, `kill-time-distribution`, `boss-spec-usage`, `comp-samples`, and `ability-usage-summary` operate on bounded report cohorts. Their scope flags are part of the trust contract:

- guild filters: `--guild-region`, `--guild-realm`, `--guild-name`
- report budget: `--report-pages`, `--reports-per-page`
- time filters: `--start-time`, `--end-time`
- encounter filters: `--zone-id`, `--boss-id`, `--boss-name`, `--difficulty`
- participant filter: `--spec-name` keeps sampled kills that include that spec; it is not a spec leaderboard

Keep sample size, exclusions, truncation, freshness, and citations with any downstream analysis.

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
- `--introspect`: run the built-in introspection query and return `introspection`

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

Partial GraphQL errors with useful data are emitted as `graphql_warnings` plus `notes`, matching typed command behavior.

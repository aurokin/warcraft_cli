# Raider.IO CLI

`raiderio` queries the Raider.IO developer API (`https://raider.io/api/v1`) for character profiles,
guild profiles, and Mythic+ leaderboard analytics. Requests are unauthenticated and cached on disk.

Design notes and the pre-implementation research record live in
[../architecture/history/raiderio.md](../architecture/history/raiderio.md).

## Output Contract

Every command writes one JSON object. It carries the shared envelope
(`ok`, `provider`, `command`, `kind`, `schema_version`, `query`, `provenance`, `data`, `error`)
described in [../foundation/ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md). The payload keys
inside `data` are also copied to the top level for agents that already read them; those flat copies
are deprecated, so read `data`.

Exit codes: `0` success, `1` configuration failure, `2` usage error (bad flag value or
`invalid_query`), `3` upstream HTTP 401/403, `4` target not found, `5` network or upstream failure. Failures write the error
envelope to stderr and never a traceback.

## Global Flags

Global flags go before the subcommand: `--pretty`, `--compact`, `--compact-max-chars N`,
`--fields <dot.path>`, `--fields-strict`, `--profile agent|human|debug`.

```bash
raiderio --fields data.results --pretty search "liquid"
```

## Commands

| Command | Arguments | Flags |
| --- | --- | --- |
| `doctor` | | |
| `search` | `QUERY` | `--limit` (1-50, default 5), `--kind all\|character\|guild` |
| `resolve` | `QUERY` | `--limit` (1-50, default 5), `--kind all\|character\|guild` |
| `character` | `REGION REALM NAME` | |
| `guild` | `REGION REALM NAME` | |
| `mythic-plus-runs` | | `--season`, `--region`, `--dungeon`, `--affixes`, `--page` |
| `leaderboard mythic-plus` | | scope flags, `--limit` (1-200, default 20) |
| `sample mythic-plus-runs` | | scope flags, `--pages`, `--limit`, filter flags |
| `sample mythic-plus-players` | | scope flags, `--pages`, `--limit`, `--player-limit`, filter flags |
| `distribution mythic-plus-runs` | | `--metric`, scope flags, `--pages`, `--limit`, filter flags |
| `distribution mythic-plus-players` | | `--metric`, scope flags, `--pages`, `--limit`, `--player-limit`, filter flags |
| `threshold mythic-plus-runs` | | `--metric`, `--value` (required), `--nearest`, scope flags, `--pages`, `--limit`, filter flags |

Scope flags (all Mythic+ commands): `--season` (slug, or empty/`current` for the Raider.IO current
default season), `--region` (default `world`), `--dungeon` (default `all`), `--affixes`, `--page`.
Sampled commands add `--pages` (1-10) and `--limit` (1-200).

Filter flags (sampled commands): `--level-min`, `--level-max`, `--score-min`, `--score-max`, and the
repeatable `--contains-role`, `--contains-class`, `--contains-spec`, `--player-region`. Filters run
after sampling; the payload reports `source_run_count`, `returned_run_count`, and
`excluded_run_count` so a narrowed slice stays provenance-safe.

Metrics:

- `distribution mythic-plus-runs --metric`: `mythic_level`, `dungeon`, `role`, `player_region`, `class`, `spec`, `composition`, `class_composition`
- `distribution mythic-plus-players --metric`: `appearance_count`, `top_mythic_level`, `class`, `spec`, `role`, `player_region`
- `threshold mythic-plus-runs --metric`: `score`, `mythic_level`

## Examples

```bash
raiderio doctor
raiderio search "liquid"
raiderio resolve "us illidan Cotti"
raiderio character us illidan Cotti
raiderio guild us illidan Liquid
raiderio leaderboard mythic-plus --season current --region us --dungeon all --limit 20
raiderio sample mythic-plus-runs --region us --limit 100 --contains-class demon-hunter
raiderio distribution mythic-plus-runs --metric mythic_level --season current
raiderio threshold mythic-plus-runs --metric score --value 3000
```

## Behavior Notes

- `search` and `resolve` probe the profile endpoints directly when the query parses as
  `<region> <realm> <name>`, and fall back to the live site search surface otherwise. `resolve`
  returns a single `match` plus `next_command` only when the top candidate is confidently ahead.
- Every Mythic+ payload echoes `resolved_season`, so the season a sample actually used is explicit.
- Sampled payloads carry `freshness` (`sampled_at`, `cache_ttl_seconds`) and `citations`
  (leaderboard URLs); those also form the envelope's `provenance` block.
- Character, guild, search, and roster rows carry a normalized `class_spec_identity` sibling that
  claims `high` confidence only when both class and spec are known.
- `raiderio_cli.provider.PROVIDER` exposes `search`, `resolve`, and `doctor` in-process for the
  `warcraft` wrapper; the Typer commands are thin wrappers over it.

## Limits

- Unauthenticated requests are limited to 200 per minute by Raider.IO. App-key support is deferred.
- The site search route used by `search`/`resolve` fallback is not part of the documented
  `/api/v1` surface.

## Source Links

- `https://raider.io/api`
- `https://raider.io/openapi.json`
- [Roadmap](../ROADMAP.md)

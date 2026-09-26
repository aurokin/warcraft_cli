# Raider.IO CLI

`raiderio` queries the Raider.IO developer API (`https://raider.io/api/v1`) for character profiles,
guild profiles, guild raid rankings, and Mythic+ leaderboard analytics. Requests are unauthenticated
and cached on disk. Guild raid leaderboards here replace the retired WowProgress provider.

## Output Contract

Every command writes one JSON object. It carries the shared envelope
(`ok`, `provider`, `command`, `kind`, `schema_version`, `query`, `provenance`, `data`, `error`)
described in [../foundation/ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md), and nothing else at
the top level: every payload field is under `data`.

Exit codes: `0` success, `1` configuration failure, `2` usage error (bad flag value, including
`invalid_argument` for an unsupported `--kind`, or `invalid_query`), `3` upstream HTTP 401/403, `4`
target not found, `5` network or upstream failure. Failures write the error envelope to stderr and
never a traceback.

## Global Flags

Global flags go before the subcommand: `--pretty`, `--compact`, `--compact-max-chars N`,
`--fields <dot.path>`, `--fields-strict`, `--profile agent|human`.

```bash
raiderio --fields data.results --pretty search "liquid"
```

## Commands

| Command | Arguments |
| --- | --- |
| `doctor` | |
| `search` | `QUERY` |
| `resolve` | `QUERY` |
| `character` | `REGION REALM NAME` |
| `guild` | `REGION REALM NAME` |
| `mythic-plus-runs` | |
| `leaderboard mythic-plus` | |
| `leaderboard raids` | |
| `raids` | |
| `sample mythic-plus-runs` | |
| `sample mythic-plus-players` | |
| `distribution mythic-plus-runs` | |
| `distribution mythic-plus-players` | |
| `threshold mythic-plus-runs` | |

Flags, defaults, and ranges are in [reference/raiderio.md](../reference/raiderio.md).
`leaderboard raids --realm` takes a slug or a display name and needs a standard region;
`raids --expansion-id` defaults to 11 (Midnight; 10 is The War Within, 9 Dragonflight).

Scope flags (all Mythic+ commands): `--season` (slug, or empty/`current` for the Raider.IO current
default season), `--region` (default `world`), `--dungeon` (default `all`), `--affixes`, `--page`.
Sampled commands add `--pages` (1-10) and `--limit` (1-200).

Filter flags (sampled commands): `--level-min`, `--level-max`, `--score-min`, `--score-max`, and the
repeatable `--contains-role`, `--contains-class`, `--contains-spec`, `--player-region`. Bounds are
inclusive ("at or above" / "at or below"), and a run whose level or score Raider.IO omitted is
excluded whenever the matching bound is set. Each `--contains-*` flag matches any roster entry on
its own, so `--contains-class priest --contains-spec holy` also keeps a Holy Paladin + Shadow Priest
run. `--contains-spec` takes a bare spec (`holy`, any class) or a class-qualified one
(`priest-holy`), which is how to ask for one class's spec. Filters run after sampling; the payload reports
`source_run_count`, `returned_run_count`, and `excluded_run_count` so a narrowed slice stays
provenance-safe.

Metrics:

- `distribution mythic-plus-runs --metric`: `mythic_level`, `dungeon`, `role`, `player_region`, `class`, `spec`, `composition`, `class_composition`
- `distribution mythic-plus-players --metric`: `appearance_count`, `top_mythic_level`, `class`, `spec`, `role`, `player_region`
- `threshold mythic-plus-runs --metric`: `score`, `mythic_level`

Spec names repeat across classes (holy, protection, restoration, frost), so every spec label is
class-qualified: the `spec` metric, the `composition` keys (`healer:priest-holy`), and a player's
`spec_slugs` tags read `priest-holy`, not `holy`. Roster rows keep the raw `class_slug` and `spec_slug`.

## Raid Leaderboards

`raiderio raids` lists the raid slugs Raider.IO knows for one expansion (kind `raid_catalog`).
Each row carries `id`, `slug`, `name`, `short_name`, per-region `starts`/`ends` timestamps, and the
`encounters` (`id`, `slug`, `name`) in order. Use it to discover the `--raid` value; slugs change
every tier, so never hard-code one. The `starts`/`ends` window also identifies the tier a guild is
currently progressing, which pairs with the `raid_slug` keys in a guild payload's `progression` rows.
The payload carries `freshness` (`fetched_at`, `cache_hit`, `cache_ttl_seconds`) and `citations`
(`static_data_url`), mirrored into the envelope's `provenance`.

`raiderio leaderboard raids --raid <slug>` returns guild rankings for one raid and difficulty
(kind `raid_leaderboard`). The payload:

- `query`: `raid`, `difficulty`, `region`, `realm` (`null` unless set), `page`, `limit`.
  `--region` takes the same aliases as the other commands (`na` -> `us`), and `--realm` takes a
  display name or a slug: `Tarren Mill` and `tarren-mill` both scope to the same realm, and the
  echoed `query.realm` plus the citation URL always carry the slug. Realms with no ASCII slug
  (`Ревущий фьорд`) are sent as written -- Raider.IO accepts them -- and percent-encoded in the
  citation URL.
- `count` and `sample` (`requested_limit`, `returned_row_count`, `pages_requested`,
  `pages_fetched`, `limit_reached`). Rankings are read in 20-row pages starting at `--page`, so
  `--limit 50` fetches up to three pages; `limit_reached: false` means the scope ran out of ranked
  guilds, not that a cap was applied silently.
- `rows`: one per guild with `rank`, `region_rank`, `guild` (`name`, `realm` slug,
  `realm_name`, `region`, `faction`, `profile_url` on raider.io), `encounters_defeated_count`,
  `encounters_pulled_count`, `encounters_defeated` (`slug`, `first_defeated`, `last_defeated`), and
  `encounters_pulled` (`slug`, `num_pulls`, `best_percent`, `is_defeated`, `pull_started_at`).
  `rank` is relative to the requested scope: world position for `--region world`, region position
  for a region, and realm position when `--realm` is set. `region_rank` is always region-wide.
  There is no separate realm-rank field: Raider.IO does not send one on this endpoint.
- `freshness` (`sampled_at`, `fetched_at`, `cache_hit`, `cache_ttl_seconds`) and `citations`
  (`leaderboard_urls`, the raider.io rankings page for the scope), mirrored into the envelope's
  `provenance`. With `--realm` the URL is the region page plus `?realm=<slug>`; whether the site
  applies that filter is unverified (the page renders client-side), so the rows, not the page, are
  the realm-scoped record.

An unknown raid slug is a usage error (exit 2) because Raider.IO rejects it as invalid input.

## Examples

```bash
raiderio doctor
raiderio search "liquid"
raiderio resolve "us illidan Cotti"
raiderio character us illidan Cotti
raiderio guild us illidan Liquid
raiderio leaderboard mythic-plus --season current --region us --dungeon all --limit 20
raiderio raids --expansion-id 11
raiderio leaderboard raids --raid liberation-of-undermine --difficulty mythic --region us --limit 20
raiderio leaderboard raids --raid liberation-of-undermine --region us --realm malganis --limit 10
raiderio sample mythic-plus-runs --region us --limit 100 --contains-class demon-hunter
raiderio distribution mythic-plus-runs --metric mythic_level --season current
raiderio threshold mythic-plus-runs --metric score --value 3000
```

## Behavior Notes

- `search` and `resolve` probe the profile endpoints directly when the query parses as
  `<region> <realm> <name>`, and fall back to the live site search surface otherwise. A realm of up
  to three words is handled (`eu tarren mill Cotti`, `us area 52 Roguecane`), and the realm may be
  spelled as a display name or as either slug form (`mal'ganis`, `mal-ganis`, `malganis`) -- all of
  them score the same, so the `next_command` a `resolve` emits resolves when it is fed back in.
  `resolve` returns a single `match` plus `next_command` only when the top candidate is confidently
  ahead of every other candidate; `--limit` only trims the `candidates` list, so `--limit 1` never
  hides a rival. `next_command`, `follow_up.command` and `fallback_search_command` are shell-quoted
  (`raiderio guild us illidan 'Liquid Guild'`). Character rows link their raider.io page in
  `profile_url` even though site search sends a path for guilds only. A *leading* `guild`/`character` word is read as a type hint and dropped
  (`guild us malganis gn`); anywhere else the word is part of the name and is kept, because
  entities are named after it (Raider.IO has a guild called `Liquid Guild`). That word only narrows
  the lookups and adds to the score. An explicit `--kind character|guild` wins over it and filters
  every candidate: when nothing of that kind matches, `search` returns no rows and `resolve` is not
  resolved, rather than answering with the other kind.
- An unknown character, guild, or realm is `not_found` (exit 4): Raider.IO answers all three with
  HTTP 400 ("Could not find requested ...", "Failed to find realm ..."). A body that is not JSON is
  `upstream_error` (exit 5).
- Every Mythic+ payload echoes `resolved_season`, so the season a sample actually used is explicit.
- Every payload with provenance carries `freshness` and `citations`; those also form the envelope's
  `provenance` block. `freshness.fetched_at` is when the response came off the wire, so a replay
  reports the age of what it replayed and `cache_hit: true` says it is a replay; the data can be up
  to `cache_ttl_seconds` old. Sampled commands add `sampled_at` (when the sample was assembled) and
  report the *oldest* page's `fetched_at`, with `cache_hit: true` when any page was replayed.
  `character` and `guild` report the same block, each quoting its own profile TTL.
- The envelope's `command` is the full sub-path (`leaderboard raids`, `distribution
  mythic-plus-runs`), on success and on failure, so no two commands answer to the same value. That
  includes a flag rejected before the command body runs (a bad value, a missing required option).
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

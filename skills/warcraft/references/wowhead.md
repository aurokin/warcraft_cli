# Wowhead

## Best For

- item, quest, spell, npc, faction, and guide lookup
- comments and linked-entity traversal
- Wowhead timelines:
  - `news`
  - `blue-tracker`
- stable tool-state inspection:
  - `talent-calc`
  - `profession-tree`
  - `dressing-room`
  - `profiler`

## Start With

- unknown object: `wowhead search "<query>"`
- conservative next step: `wowhead resolve "<query>"`
- known entity: `wowhead entity <type> <id>`
- known guide: `wowhead guide <id-or-url>`
- timeline scan: `wowhead news ...` or `wowhead blue-tracker ...`

## Effective Use

- prefer `entity` first, then `entity-page` only when you need fuller linked-entity context
- use `comments` when you need more than the default embedded comment slice
- use `guides <category>` when the guide family is known but the exact guide is not
- use `guide-full` or `guide-export` when you need the raw guide body plus additive `analysis_surfaces` for comparison-oriented workflows
- use `guide-query --kind analysis_surfaces` when you want section-backed guide topics without discarding the underlying guide text
- use timeline filters like `--author`, `--type`, `--region`, and `--forum` instead of scanning broad result sets manually
- use guide filters like `--author`, `--updated-after`, `--patch-min`, and `--sort`
- use `news-post` and `blue-topic` once you already have a specific URL
- filter timelines by date with `--date-from` / `--date-to`, and read each row's ISO `posted_at`
  rather than the rendered `posted` string; rows Wowhead timestamps in a form the CLI cannot read
  are left out of the window and counted in `scan.unparsed_timestamps`
- `resolve --entity-type` covers the types Wowhead's suggestion endpoint labels; mounts, recipes,
  and battle pets are not among them and come back as items, spells, or NPCs
- read `count` as the rows you were given and `total_matches` / `total` as what the limit cut off;
  raise `--limit` when `truncated` is true
- guides far older than the freshest guide in the same response carry `stale_guide` in
  `ranking.match_reasons` and are listed after every current row that matches the query as closely;
  a retired guide leads only when it matches more closely than every current row (its exact title,
  say). `resolve` never answers one with
  high confidence, so run the `fallback_search_command` when it does not resolve
- `search` and `resolve` rank on Wowhead's own database and guide ordering first, so the entity a
  query names leads the proc spells and secondary rows that share its name, and a class-guide query
  resolves to the main current guide; `ranking.match_reasons` carries `upstream_database_rank` on
  the rows that ordering promoted
- `search` and `resolve` rank every row Wowhead's suggestion response sent, not just its ten-row
  dropdown list, one row per entity; `metadata.suggestion_lists` says where each row came from and
  `suggestion_merge` counts the rows per list and the duplicates merged
- query words match whole words, ignoring words like "the" and "of"; a row is returned only when its
  text holds every query word or the whole query, or Wowhead's own ordering ranked it near the top
  and its name shares a query word. Rows matching only some words are dropped, and
  `suggestion_merge.unmatched_rows_dropped` counts them
- `resolve` answers with a database entity: news posts and world events sit behind every entity in
  `candidates` and become the `match` only when the response holds no entity, or when the article
  outscores the best entity by a wide margin (a query that names a headline word for word); use
  `search` when you want the news coverage ranked on its own merits

## Boundaries

- database-family browse/filter pages are intentionally deferred
- `dressing-room` and `profiler` are state inspectors, not full decoders
- do not assume Wowhead tool URLs expose enough stable state for deep reverse-engineering
- treat Wowhead `analysis_surfaces` as an additive page-level layer extracted from trusted section structure, not as a replacement for the raw guide page

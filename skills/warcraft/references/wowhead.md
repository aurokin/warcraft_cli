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
- `resolve` drops to medium confidence with no `next_command` when its best guide match is far
  older than the other guides in the same response; run the `fallback_search_command` instead
- `resolve` answers with a database entity: news posts and world events sit behind every entity in
  `candidates` and only become the `match` when the response holds no entity at all; use `search`
  when you want the news coverage ranked on its own merits

## Boundaries

- database-family browse/filter pages are intentionally deferred
- `dressing-room` and `profiler` are state inspectors, not full decoders
- do not assume Wowhead tool URLs expose enough stable state for deep reverse-engineering
- treat Wowhead `analysis_surfaces` as an additive page-level layer extracted from trusted section structure, not as a replacement for the raw guide page

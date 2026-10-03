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

- prefer `entity` first, then `entity-page` only when you need fuller linked-entity context; both
  include the page's relation tabs (a zone's NPCs and quests, a faction's members) as
  `source_kind: "listview"` links, and both take `--url <Wowhead entity URL>` in place of `<type> <id>`
- use `comments` when you need more than the default embedded comment slice
- use `guides <category>` when the guide family is known but the exact guide is not
- use `guide-full` or `guide-export` when you need the raw guide body plus additive `analysis_surfaces` for comparison-oriented workflows
- use `guide-query --kind analysis_surfaces` when you want section-backed guide topics without discarding the underlying guide text
- `guide-query` answers like `icy-veins guide-query` and `method guide-query`: per-kind `match_counts` and
  `matches` (whole bundle rows plus `kind` and `score`), a flattened `top`, and `failed_pages`;
  `--linked-source href|gatherer|multi` narrows linked entities by where the page linked them
- use timeline filters like `--author`, `--type`, `--region`, and `--forum` instead of scanning broad result sets manually
- the `news`, `blue-tracker` and `guides` query keeps a row only when every query word is a whole word
  in it, up to a plural or possessive ending, so "hotfix" matches "Hotfixes" but "frost mage" does not
  match "Frost Death Knight" or "Damage"
- use guide filters like `--author`, `--updated-after`, `--patch-min`, and `--sort`
- use `news-post` and `blue-topic` once you already have a specific URL; `news-post` takes only
  `/news/...` or `/news=<id>` pages and `blue-topic` only `/blue-tracker/topic/...` pages, so the
  `/blue-tracker/news/...` rows of `blue-tracker` feed neither
- a `/forever/` (WoW Forever) URL names no expansion this CLI reads: `entity --url` refuses it, and
  `news-post` reads a Forever post with a `notes` entry saying the expansion could not be inferred
- a news date window far in the past needs enough `--pages` to reach it; `scan.stop_reason: null`
  means `--pages` ran out before the scan got there
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
  resolves to the main current guide (the guide ordering counts only when the query says "guide" or `--entity-type guide` is set).
  When the query names a type ("bm hunter guide") and the top row is another type holding only some
  of its words, `resolve` is not confident and does not resolve; read `candidates`. The same holds
  when the top row lacks a number the query names ("season 3" against "Season 2");
  `ranking.match_reasons` carries `upstream_database_rank` on the rows that ordering promoted
- `search` and `resolve` rank every row Wowhead's suggestion response sent, not just its ten-row
  dropdown list, one row per entity; `metadata.suggestion_lists` says where each row came from and
  `suggestion_merge` counts the rows per list and the duplicates merged
- `search <Wowhead entity URL>` answers with that entity alone (`name` null, `follow_up` set); use
  `entity` for its details. A guide, news, blue-tracker, tool or listing URL answers with the one
  command that reads it (`follow_up.command`); `resolve <url>` returns it as `next_command`; any
  other Wowhead URL fails `invalid_query`
- follow-up words ("comments", "links", "full") steer `follow_up.command` and are not searched for,
  unless the whole query is a name ("Soul Link")
- query words match whole words, ignoring words like "the" and "of"; a row whose text holds none of
  the query words is not returned, and `suggestion_merge.unmatched_rows_dropped` counts those rows.
  A row holding only some of them stays (`some_terms_match`) and scores less for the words it lacks,
  but Wowhead's own ordering bonus can still rank it above a row holding them all. A row's type name
  counts as its text, so a type word such as "npc" in the query keeps every NPC row
- `resolve` answers with a database entity: news posts and world events sit behind every entity in
  `candidates` and become the `match` only when the response holds no entity, or when the article
  outscores the best entity by a wide margin (a query that names a headline word for word); use
  `search` when you want the news coverage ranked on its own merits

## Boundaries

- database-family browse/filter pages are intentionally deferred
- `dressing-room` and `profiler` are state inspectors, not full decoders; `profiler` fails with
  `not_found` when Wowhead says the list does not exist
- on a retail, PTR or beta ref, `talent-calc` rejects a spec that is not the class's and a build
  code whose loadout header names another spec (`invalid_tool_ref`); classic calculators keep their
  own spec names (MoP Classic rogue `combat`); `listed_builds` are the ref's spec's builds only
- do not assume Wowhead tool URLs expose enough stable state for deep reverse-engineering
- treat Wowhead `analysis_surfaces` as an additive page-level layer extracted from trusted section structure, not as a replacement for the raw guide page

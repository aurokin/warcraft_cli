# Wowhead CLI

`wowhead` queries Wowhead over plain HTTP and returns JSON. It does no browser automation and
needs no credentials.

Companion docs:
- [ACCESS_METHODS.md](ACCESS_METHODS.md)
- [CONTRACTS.md](CONTRACTS.md)
- [EXPANSION_RESEARCH.md](EXPANSION_RESEARCH.md)
- [NORMALIZATION.md](NORMALIZATION.md)
- [History and design record](../architecture/history/wowhead.md)

## Output Contract

Every command prints one JSON document. Successful payloads carry the shared envelope keys
(`ok`, `provider`, `command`, `kind`, `schema_version`, `query`, `provenance`, `data`) alongside
Wowhead's historical top-level keys such as `results`, `entity`, `comments`, and `linked_entities`.
`data` carries those same keys; the top-level copies are deprecated. With `--stream`, the JSONL
header empties the streamed collection in both places.

Nothing is dropped silently. In a result list, `count` is the number of rows returned and the block
also reports the pre-limit total — `total` for link lists, `total_matches` for `search`, `resolve`,
`news`, `blue-tracker`, `guides`, and `guide-bundle-search` — next to a `truncated` flag. Blocks
that deliberately return a sample instead (`comments`, the linked-entity preview,
`analysis_surfaces`) report the full `count` alongside `more_available` / `needs_raw_fetch` and a
`fetch_more_command`.

Listing rows expose both what Wowhead rendered and a machine-readable timestamp: `posted` is the
upstream string (`news` renders "2026/09/18 at 3:30 PM", `blue-tracker` sends
"2026-09-18 18:48:08") and `posted_at` is the same instant as an ISO 8601 UTC value, which is what
`--date-from` / `--date-to` compare against. Wowhead writes both forms in US Central with no
offset, so `posted_at` is shifted accordingly. A row whose timestamp cannot be parsed is excluded
from a date window rather than passed through, and `scan.unparsed_timestamps` reports how many rows
that was; when a date window is requested and no scanned row carries a readable timestamp, the
command fails with `parse_error` instead of returning an empty match set.

`search` results carry `entity_type` and an openable `url` for every type Wowhead's suggestion
endpoint labels. News posts also carry a `news-post` follow-up; world events are openable but have
no follow-up command of their own. The one exception is Trading Post activities: Wowhead addresses
them only by slug, so those rows come back with a null `url`.

Ranking starts from Wowhead's own ordering. A suggestion response carries two views of the same
rows: the flat `results` list, ordered by the `popularity` ordinal, and a `categories.database`
list, ordered by relevance. `search` and `resolve` score the leading rows of the database list up
(`upstream_database_rank` in `ranking.match_reasons`) so the entity a query names leads the proc
spells and secondary rows that share its name, then text evidence — exact name, prefix, term
coverage, type hints — decides the rest. Rows Wowhead returns only in the flat list are ranked on
text alone.

`resolve` answers with a database entity. A news headline often matches a query better than the
item it is written about, so news posts and world events rank behind every entity in `candidates`.
They become the `match` only when the response holds no entity, or when the article outscores the
best entity by more than an exact name match is worth, which is how a query that names a headline
word for word still resolves to that news post. `search` ranks them on score alone.

Failures print an error envelope on stderr and exit with the shared code:

| Exit | Meaning |
|------|---------|
| 0 | success |
| 1 | generic failure (parse errors, unexpected upstream payloads, bad cache config) |
| 2 | usage error (bad flag value, rejected filter, invalid date range) |
| 3 | authentication failure |
| 4 | upstream 404 |
| 5 | transport failure, timeout, or other upstream HTTP error |

Full contract: [docs/foundation/ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md).

## Global Flags

Global flags go before the subcommand.

| Flag | Effect |
|------|--------|
| `--pretty` | pretty-print JSON instead of the compact default |
| `--compact` | truncate long strings to shrink the payload |
| `--compact-max-chars N` | truncation threshold for `--compact` (default 280) |
| `--fields a.b,c` | project only the named dot-paths |
| `--fields-strict` | fail with exit 2 when a requested `--fields` path is missing |
| `--profile agent\|human` | output preset; `agent` is compact JSON, `human` pretty-prints |
| `--stream` | emit large arrays as JSONL: a header line then one `{"record": ...}` per row |
| `--expansion KEY` | route to an expansion profile (`retail`, `classic`, `tbc`, `wotlk`, `cata`, `mop-classic`, `ptr`, `beta`, `classic-ptr`) |
| `--normalize-canonical-to-expansion` | rewrite canonical entity URLs back to the selected expansion |
| `--citation-pack` | attach a deterministic `citation_pack` of source URLs and per-claim anchors |

Example:

```bash
wowhead --pretty --expansion classic entity item 19019
```

When `--expansion` is omitted, `search` (from its query), `entity` and `entity-page` (from `--url`),
and `compare` (from its entity refs) auto-detect the profile from a Wowhead URL; the payload reports
which rule applied in `expansion_source` (`flag`, `url`, or `default`).

## Commands

Discovery and routing:

| Command | Purpose |
|---------|---------|
| `search QUERY` | ranked entity candidates from Wowhead search suggestions |
| `resolve QUERY` | the single most likely entity plus the follow-up command to run |
| `expansions` | supported expansion profiles and their routing |
| `expansion-detect URL` | which profile a Wowhead URL belongs to |
| `doctor` | endpoint reachability, parser shape checks, cache readiness |

Entities:

| Command | Purpose |
|---------|---------|
| `entity TYPE ID` | tooltip payload, optionally with comments and a linked-entity preview; `--include-all-comments` replaces the `comments.top` summary with the full `comments.items` list |
| `entity-page TYPE ID` | parsed page metadata and linked entities; comments come from `comments` |
| `comments TYPE ID` | ranked comments with filters and optional insight rollups |
| `compare REF REF ...` | field-by-field diff of two or more entities |
| `linked-graph TYPE ID` | bounded linked-entity graph rooted at one entity |

Guides:

| Command | Purpose |
|---------|---------|
| `guides CATEGORY` | guide listing for a category with author, patch, and updated-window filters |
| `guide REF` | one guide: analysis surfaces, linked entities, comments, and page metadata; section bodies come from `guide-full`. An unknown guide id is an upstream 400, reported as exit 5 |
| `guide-full REF` | the same guide with every section, comment, and link hydrated |
| `guide-export REF` | write a guide bundle (manifest, sections, entities) to `--out`, or `./wowhead_exports/<guide-slug>/` |
| `guide-query BUNDLE QUERY` | query one guide bundle for matching sections, links, and comments |
| `guide-bundle-list` | local bundles with freshness and hydration summaries |
| `guide-bundle-search QUERY` | find local bundles by title, id, or directory name |
| `guide-bundle-query QUERY` | rank matches across every local bundle |
| `guide-bundle-inspect REF` | missing files, stale data, and hydration gaps for one bundle |
| `guide-bundle-index-rebuild` | rebuild the corpus index from bundles on disk |
| `guide-bundle-refresh REF` | re-export stale bundles using their recorded export options |

Timeline surfaces:

| Command | Purpose |
|---------|---------|
| `news [QUERY]` | news listing with topic, date-window, author, and type filters; rows may carry expansion-scoped URLs such as `/forever/news/<slug>-<id>`, which `news-post` accepts |
| `news-post REF` | one news article with body markup, related posts, and citations |
| `blue-tracker [QUERY]` | blue-post listing with topic, date-window, author, region, and forum filters; rows mix `/blue-tracker/topic/...` and `/blue-tracker/news/...` shapes, and only topic rows feed `blue-topic` |
| `blue-topic REF` | one blue-tracker topic with posts, participants, and citations |

Tool-state decoders:

| Command | Purpose |
|---------|---------|
| `talent-calc REF` | class, spec, and build code from a talent calculator ref |
| `talent-calc-packet REF` | exact talent transport packet; `--out PATH` writes just the packet |
| `profession-tree REF` | profession slug and loadout code |
| `dressing-room REF` | normalized share hash and cited state URL |
| `profiler REF` | normalized `list=` ref with list, region, realm, and name parts |

`dressing-room` and `profiler` are state inspectors: they normalize and cite the ref, they do not
decode the opaque client-side payload behind it.

Cache maintenance:

| Command | Purpose |
|---------|---------|
| `cache-inspect` | backend configuration and per-namespace entry counts |
| `cache-repair` | delete unreadable or expired file-cache entries (`--apply` to write) |
| `cache-clear` | clear cached responses for chosen namespaces or all of them |

Run `wowhead <command> --help` for the full flag list of any command.

## Workflows

Resolve, then fetch:

```bash
wowhead resolve "thunderfury"
wowhead entity item 19019
```

Export a guide once, query it repeatedly offline:

```bash
wowhead guide-export 2113 --out ./tmp/guides/guide-2113
wowhead guide-query ./tmp/guides/guide-2113 "talent build"
```

Scan a topic across a date window:

```bash
wowhead news "class tuning" --date-from 2025-01-01 --date-to 2025-03-01 --pages 3
```

## Cache

The HTTP cache is configured from the environment:

| Variable | Meaning |
|----------|---------|
| `WOWHEAD_CACHE_BACKEND` | `file`, `redis`, or `none` |
| `WOWHEAD_CACHE_DIR` | file-cache directory |
| `WOWHEAD_REDIS_URL` | Redis URL; required when the backend is `redis` |
| `WOWHEAD_REDIS_PREFIX` | Redis key prefix |
| `WOWHEAD_*_CACHE_TTL_SECONDS` | per-namespace TTL overrides (`SEARCH`, `TOOLTIP`, `ENTITY_PAGE`, `GUIDE_PAGE`, `PAGE`, `COMMENT_REPLIES`, `ENTITY`) |

`cache-inspect` reports the resolved settings, and `doctor` includes them in its payload.

## Source Links

- [Usage](../USAGE.md)
- [Access Methods](ACCESS_METHODS.md)
- [Expansion Research](EXPANSION_RESEARCH.md)

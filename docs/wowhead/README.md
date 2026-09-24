# Wowhead CLI

`wowhead` queries Wowhead over plain HTTP and returns JSON. It does no browser automation and
needs no credentials.

Companion docs:
- [CONTRACTS.md](CONTRACTS.md)
- [NORMALIZATION.md](NORMALIZATION.md)

## Output Contract

Every command prints one JSON document: the shared envelope (`ok`, `provider`, `command`, `kind`,
`schema_version`, `query`, `provenance`, `data`, plus `error` on failure) and nothing else at the
top level. Command output such as `results`, `entity`, `comments`, and `linked_entities` lives
under `data`, so `--fields` paths start there (`--fields data.results`). With `--stream`, the JSONL
header is the envelope with the streamed collection emptied and `data.stream` naming it.

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

A suggestion response carries two overlapping row lists: the flat `results` list (the dropdown's
~10 rows, ordered by the `popularity` ordinal) and a `categories` map (`database`, `guides`, `news`,
...) that often holds rows `results` leaves out, sometimes the very entity a query names.
`search` and `resolve` rank the union, one row per Wowhead type and id. Each row's
`metadata.suggestion_lists` names the lists it came from (`metadata.popularity` is null for a row
only `categories` carried), and `suggestion_merge` reports the rows each list sent and how many
duplicates the merge removed. A merged row whose text holds no query word (no
`ranking.match_reasons` beyond `type_hint` or `stale_guide`) is not returned, and
`suggestion_merge.unmatched_rows_dropped` counts those rows; `total_matches` counts the rows kept.
A row holding only some query words stays, with `some_terms_match`, and scores less for each word it
lacks; Wowhead's own ordering bonus (below) can still rank it above a row that holds them all. The
row's type name (`typeName`) counts as row text, so a type word such as "npc" in the query keeps
every NPC row as a partial match.

Ranking starts from Wowhead's own ordering. `categories.database` and `categories.guides` are
ordered by relevance, and `search` and `resolve` score the leading rows of each up
(`upstream_database_rank` in `ranking.match_reasons`), so the entity a query names leads the proc
spells and secondary rows that share its name, and "fury warrior guide" resolves to the main
current guide rather than the five others that share its words. The guides order counts only when
the caller asks for a guide ("guide" or "guides" in the query, or `--entity-type guide`), so it cannot narrow the lead of the entity an
entity query names. Text evidence (exact name,
prefix, term coverage, type hints) decides the rest. Query words match whole words only, and
"the", "of", "a", "an", "and", "in", "on", "for" and "to" are not matched at all. The rank bonus
needs a query word in the row's own name, or a name that starts with or contains the query
("valorstone" and "Valorstones"): Wowhead also ranks rows on text the suggestion never shows, and
those get no bonus. A name that merely contains the query scores below one that starts
with it. Each row's `follow_up.command` is the command to run next.

Follow-up words in a query ("comments", "links", "full", "related", ...) pick the follow-up command
(`comments`, `entity-page`) and are left out of the text sent to Wowhead, which `search_query`
reports. A query that is itself a name made of such words ("Soul Link", "Body and Soul") is sent
whole first, and kept when a row carries exactly that name.

`search` given a Wowhead entity URL (`https://www.wowhead.com/classic/item=19019/...`) answers with
that entity alone: one row with its type, id, URL and `follow_up`, `match_reasons: ["url_entity"]`,
`name: null` (nothing is fetched), and `search_query: null`. Wowhead's suggestions endpoint matches
names, so it has nothing to say about a URL.

A guide updated far (180+ days) behind the freshest guide in the same response carries
`stale_guide` in `ranking.match_reasons` and sorts after every current row that matches the query
at least as closely (exact name, then a name starting with the query, then any other match),
whatever its score. A class-guide query leads with the current guide rather than a retired
event's, while a query that names the retired guide ("Fury Warrior PvP Guide") still leads with it.

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
| 2 | usage error (bad flag value, rejected filter, invalid date range, a malformed talent-calc or tool reference (`invalid_tool_ref`) or news or blue-tracker reference (`invalid_ref`)) |
| 3 | authentication failure |
| 4 | upstream 404 |
| 5 | transport failure (`network_error`), `timeout`, HTTP 429 (`rate_limited`), or other upstream HTTP error (`upstream_error`) |

Every upstream HTTP failure carries `error.details.status_code` and `error.details.url`.

Full contract: [docs/foundation/ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md).

## Global Flags

Global flags go before the subcommand.

| Flag | Effect |
|------|--------|
| `--pretty` | pretty-print JSON instead of the compact default |
| `--compact` | truncate long prose strings (URLs, talent strings and `*command` values stay whole; cut paths are listed in `provenance.compacted_paths`) |
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
| `guide-bundle-refresh REF` | re-export stale bundles using their recorded export options and expansion (`--expansion` overrides it) |

Section `content_text` (and `body.summary`) is the plain text of the section markup with Wowhead's
inline tokens spelled out: `[spell=184367]` becomes the name the page's own entity data gives it
("Rampage"), and a `[build]` block keeps its title, stat priority, key talents, and listed items.
A token whose entity the page does not name is dropped; `content_raw` keeps the markup as served.

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
| `talent-calc-packet REF` | exact talent transport packet; `--out PATH` writes just the packet. The packet comes from the build code in `REF`, so a failed page fetch still answers, with `page.canonical_url` null and `page.fetch_error` `{code, message}` |
| `profession-tree REF` | profession slug and loadout code |
| `dressing-room REF` | normalized share hash and cited state URL |
| `profiler REF` | normalized `list=` ref with list, region, realm, and name parts |

`dressing-room` and `profiler` are state inspectors: they normalize and cite the ref, they do not
decode the opaque client-side payload behind it. `profiler` fetches the list page for the ref and
fails with `not_found` (exit 4) when Wowhead answers with its "This list doesn't exist or has been
removed" page. For both, `page.canonical_url` is the fetched page's own canonical link; when the
page names none it is null and `page.note` says so. A share URL under an expansion path
(`/classic/dressing-room#...`) is read from that expansion unless `--expansion` is passed.

Cache maintenance:

| Command | Purpose |
|---------|---------|
| `cache-inspect` | backend configuration and per-namespace entry counts |
| `cache-repair` | report, or with `--apply` delete, legacy entries at the file-cache root from before cache namespacing; unreadable entries elsewhere are only counted (use `cache-clear`) |
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

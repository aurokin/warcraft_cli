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
| `--profile agent\|human\|debug` | output preset; `debug` adds a `diagnostics` block |
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
| `entity TYPE ID` | tooltip payload, optionally with comments and a linked-entity preview |
| `entity-page TYPE ID` | parsed page metadata, linked entities, and comments |
| `comments TYPE ID` | ranked comments with filters and optional insight rollups |
| `compare REF REF ...` | field-by-field diff of two or more entities |
| `linked-graph TYPE ID` | bounded linked-entity graph rooted at one entity |

Guides:

| Command | Purpose |
|---------|---------|
| `guides CATEGORY` | guide listing for a category with author, patch, and updated-window filters |
| `guide REF` | one guide with sections, linked entities, and citations |
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
| `news [QUERY]` | news listing with topic, date-window, author, and type filters |
| `news-post REF` | one news article with body markup, related posts, and citations |
| `blue-tracker [QUERY]` | blue-post listing with topic, date-window, author, region, and forum filters |
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

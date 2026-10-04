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
`fetch_more_command`. `entity-page` returns at most 2000 links, so the linked-entity preview's
`fetch_more_truncated` is true when the page has more links than its `fetch_more_command` can return.

Listing rows expose both what Wowhead rendered and a machine-readable timestamp: `posted` is the
upstream string (`news` renders "2026/09/18 at 3:30 PM", `blue-tracker` sends
"2026-09-18 18:48:08") and `posted_at` is the same instant as an ISO 8601 UTC value, which is what
`--date-from` / `--date-to` compare against. Wowhead writes both forms in US Central with no
offset, so `posted_at` is shifted accordingly. A row whose timestamp cannot be parsed is excluded
from a date window rather than passed through, and `scan.unparsed_timestamps` reports how many rows
that was; when a date window is requested and no scanned row carries a readable timestamp, the
command fails with `parse_failed` instead of returning an empty match set. The listing is read
newest first, so a window far in the past needs enough `--pages` (or a later `--page`) to reach it.
`scan.stop_reason` says why the scan stopped: `date_from_reached`, `last_page_reached`,
`empty_page`, or null when `--pages` ran out first, in which case older rows were never read.

The `QUERY` of `news`, `blue-tracker` and `guides` keeps a row only when every query word appears
in it as a whole word, up to a plural or possessive ending ("hotfix" matches "Hotfixes" and "mage"
matches "Mage's", but "mage" does not match "Damage" or "Magelord"; "the" and "of" are ignored unless
the query is nothing else). A `guides` row's text includes its URL slug, so `guides raids "venomous
abyss"` keeps the raid's boss guides, whose titles name only the boss.

`search` results carry `entity_type` and an openable `url` for every type Wowhead's suggestion
endpoint labels, plus `provider` and `kind` (the entity type in snake case: `transmog_set`). News
posts also carry a `news-post` follow-up; world events are openable but have no follow-up command of
their own (`follow_up: {"command": null, "surface": "none"}`), so `resolve` never answers one with
high confidence. The one exception is Trading Post activities: Wowhead addresses
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
row's type name (`typeName`) counts as row text, so "hogger npc" holds every word of NPC "Hogger".
Words match up to a plural ending ("spirit beasts" holds "Spirit Beast"), but not a possessive
("onyxia" does not hold every word of "Onyxia's Lair"). Internal test entries
Blizzard tags "(DNT)" are left out unless the query says "dnt".

Ranking starts from Wowhead's own ordering. `categories.database` and `categories.guides` are
ordered by relevance, and `search` and `resolve` score the leading rows of each up
(`upstream_database_rank` in `ranking.match_reasons`), so the entity a query names leads the proc
spells and secondary rows that share its name, and "fury warrior guide" resolves to the main
current guide rather than the five others that share its words. The guides order counts only when
the caller asks for a guide ("guide" or "guides" in the query, or `--entity-type guide`), so it cannot narrow the lead of the entity an
entity query names. Text evidence (exact name,
prefix, term coverage, type hints) decides the rest. Query words match whole words only, and
"the", "of", "a", "an", "and", "in", "on", "for" and "to" are not matched at all. A name starts
with or contains the query only on word boundaries, up to a plural ending: "valorstone" starts
"Valorstones", but "shadow" does not start "Shadowfeather Shawl" and "frost" is not inside
"Frostsaber". The rank bonus needs a query word in the row's own name, or a name that starts with
or contains the query: Wowhead also ranks rows on text the suggestion never shows, and those get no
bonus. A name that merely contains the query scores below one that starts with it. Each row's
`follow_up.command` is the command to run next and `follow_up.surface` the command it runs
(`entity`, `entity-page`, `comments`, `guide`, `guide-full`, `news-post`).

Follow-up words in a query ("comments", "links", "full", "related", ...) pick the follow-up command
(`comments`, `entity-page`) and are left out of the text sent to Wowhead, which `search_query`
reports. A query that is itself a name made of such words ("Soul Link", "Body and Soul") is sent
whole first, and kept when a row carries exactly that name.

Wowhead matches every word it is sent against row names, so a type word ("hogger npc", "thunderfury
item") finds nothing unless the names hold it, as guide titles hold "guide". When no returned row has
a type the query names and none is named exactly the query ("Battle Pet Training" is a spell, "Mount
Parade" an achievement), the query is sent again without its type words (`search_query` "hogger"),
and that answer is kept when it holds a row of the named type. Mounts, battle pets and recipes have
no suggestion type of their own, so "mount" also names items and spells, "battle pet" NPCs and
items, and "recipe" items and spells ("Mimiron's Head mount" resolves to item 45693). Type words
still score (`type_hint`), and a row of a named type is also matched without them, so
"Heritage of the Lightforged quest" names that quest exactly (`exact_name`); guides, whose titles
hold "guide", are matched on the title alone.

`search` given a Wowhead entity URL (`https://www.wowhead.com/classic/item=19019/...`) answers with
that entity alone: one row with its type, id, URL and `follow_up`, `match_reasons: ["url_entity"]`,
the URL as its `name` (nothing is fetched), and `search_query: null`. Wowhead's suggestions endpoint matches
names, so it has nothing to say about a URL. A guide, news, blue-tracker topic, tool
(`talent-calc`, `profession-tree-calc`, `dressing-room`, profiler `list`) or listing (`/news`,
`/blue-tracker`, `/guides/<category>`) URL answers the same way with `match_reasons: ["url_page"]`,
the URL as its `id` and `name`, `kind` the page's type (`guide`, `news`) or the command that reads it
(`blue_topic`, `talent_calc`), and a `follow_up.command` that runs the command reading that page. Any other Wowhead
URL fails `invalid_query` (exit 2) with the commands that take URLs. `resolve` answers every such
URL with that row as a high-confidence `next_command`, routed to the URL's expansion, unless
`--entity-type` excludes the URL's type: then nothing resolves. A listing URL runs the listing's
first page; its query-string state (`?page=`, `?region=`) is not carried over.

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

A top row holding only some query words (`some_terms_match`) is a high-confidence answer only when
it is of a type the query names (`type_hint`) and its name holds every number the query names;
otherwise `resolve` reports it as a `medium` candidate with no `next_command`. "keystone legend
season 3" does not resolve to "Keystone Legend: Season 2", even with "achievement" added, nor "tier 2
warrior transmog" to "Sepulcher of the First Ones LFR Warrior Tier", nor "midnight season 2 mythic+
dungeons" to "Midnight Season 2: Resilient Keystone 12", but "resto druid guide" resolves to
"Restoration Druid Healer Guide" and "hogger mob" to NPC "Hogger".

A one-word `search_query` resolves at high only to a row that word names: its name or display name
whole or up to a plural ending (`valorstone` for "Valorstones"), or its name's head before the first
`,` or `:` (`thunderfury` for "Thunderfury, Blessed Blade of the Windseeker"). Wowhead's own ranking
often puts a row that merely contains the word first (`shadow` for the quest "In the Catalyst's
Shadow"); that row stays the `match` at `medium` with `confidence_cap: {"rule": "single_word_query",
"from": "high"}`, and so does a correct row the word does not name in full (`illidan` for "Illidan
Stormrage"). A URL is never capped.

Failures print an error envelope on stderr and exit with the shared code:

| Exit | Meaning |
|------|---------|
| 0 | success |
| 1 | generic failure (parse errors, unexpected upstream payloads, bad cache config) |
| 2 | usage error (bad flag value, rejected filter, invalid date range, a malformed talent-calc or tool reference (`invalid_tool_ref`) or news or blue-tracker reference (`invalid_ref`)) |
| 3 | authentication failure |
| 4 | upstream 404; an entity type this CLI does not know that Wowhead answers with a 404 or another page (`entity items 19019`, a typo) is `invalid_argument`, exit 2, listing the known types. Wowhead's tooltip endpoint also 404s on some real page types (`entity class 1`, `title`, `skill`), so that message suggests `entity-page`, which reads them |
| 5 | transport failure (`network_error`), `timeout`, HTTP 429 (`rate_limited`), or other upstream HTTP error (`upstream_error`) |

Every upstream HTTP failure carries `error.details.status_code` and `error.details.url`.

Full contract: [docs/foundation/ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md).

## Global Flags

Global flags go before the subcommand.

| Flag | Effect |
|------|--------|
| `--pretty` | pretty-print JSON instead of the compact default |
| `--compact` | truncate long prose strings (URLs, talent strings and `*command`/`*input` values stay whole; cut paths are listed in `provenance.compacted_paths`) |
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

When `--expansion` is omitted, `search` (from its query), `entity` and `entity-page` (from `--url`,
which takes the place of `TYPE ID`), `compare` (from its entity refs), and the guide, news-post,
blue-topic and tool commands (from their ref) auto-detect the profile from a Wowhead URL or a
`classic/...`-style path. `search`, `entity` and `entity-page` report which rule applied in
`expansion_source` (`flag`, `url`, or `default`). A URL under Wowhead's `/forever/` section (WoW
Forever) names no expansion profile: `expansion-detect` reports `detected_expansion: null`, an
entity URL there is not read (`entity --url` fails `invalid_argument`), and `news-post` reads a
Forever post with a note in `notes` that its expansion could not be inferred. `guide`, `guide-full`
and `news-post` add the same kind of note when the page Wowhead served belongs to another
expansion than the one selected (Wowhead answers `/classic/guide=<id>` with a retail guide).

## Commands

Discovery and routing:

| Command | Purpose |
|---------|---------|
| `search QUERY` | ranked entity candidates from Wowhead search suggestions (5 by default, `--limit` up to 50); `--entity-type` keeps only the named types, the same set `resolve --entity-type` takes, and is echoed in `filters.entity_types` |
| `resolve QUERY` | the single most likely entity plus the follow-up command to run (`--limit` lists up to 50 candidates) |
| `expansions` | supported expansion profiles and their routing |
| `expansion-detect URL` | which profile a Wowhead URL belongs to |
| `doctor` | endpoint reachability, parser shape checks, cache readiness; with the Redis backend it connects to Redis and reports `cache.available` and `cache.error`, and an unreachable or misconfigured Redis makes `status` `degraded` with `cache` in `failed_probes` |

Entities:

| Command | Purpose |
|---------|---------|
| `entity TYPE ID` | tooltip payload, optionally with comments and a linked-entity preview; `--include-all-comments` replaces the `comments.top` summary with the full `comments.items` list. `tooltip.text` spells out the units Wowhead marks in the tooltip markup: `87s 50c` for a silver and copper sell price, `Cost: 180 Darkmoon Prize Ticket` for a currency cost. The preview leads each type with the links a relation tab lists (an item's `dropped-by` NPC), marked with `listview` and `listview_data` |
| `entity-page TYPE ID` | parsed page metadata and linked entities; comments come from `comments`. Linked entities cover body links, gatherer records, and the page's relation tabs (a zone's NPCs and quests, a faction's members, an item's droppers and vendors: `source_kind: "listview"`, tab id in `listview`, every tab in `listviews`). A tab row's drop sample (`count` of `outof`) and vendor `cost` and `stock` are in `listview_data` as Wowhead sends them. A link's name is Wowhead's own when gatherer data or a tab names the entity; an all-lowercase anchor text ("this achievement") is not shown as a name in the preview |
| `comments TYPE ID` | ranked comments with filters and optional insight rollups |
| `compare REF REF ...` | field-by-field diff of two or more entities; `comparison.linked_entities` compares every link each page carries (the links `entity-page` reports, relation tabs included), not the `--max-links-per-entity` cut |
| `linked-graph TYPE ID` | bounded linked-entity graph rooted at one entity, following the links `entity-page` reports (relation tabs included, `source_kind: "listview"`); a node's `name` falls back to its page title once fetched. `--relation` takes entity types and rejects any other value. `sampling.pages_skipped` counts the pages `--max-fetches` or `--limit` left unread, and `sampling.truncated` is true when any were or `--limit` cut the nodes |

`entity` (when it reads the page) and `entity-page` add a `facts` block from the page's infobox
and map, with only the keys the page has: `quick_facts` (the Quick Facts lines as text: level,
side, type, patch; `[class=1]` reads "class 1", an NPC's React line reads "Alliance hostile, Horde
friendly", icon-only lines are left out), a quest's `start` and `end` entities (`type`, `id`, `name`,
`url`; an item that starts a quest comes as `[item=ID]` with `name` null), `series` (each chain the
page lists, quests on quest pages and achievements on achievement pages, in order: `position`,
`type`, `id`, `name`, `url`, `current` for the page's own entry), and for NPC and object pages
`locations` (`zone_id`, `zone`, map `level`, `count`). `entity-page` adds each location's `coords`
as `[x, y]` map percentages; `entity` leaves them out, since a common ore node lists thousands.

`TYPE`, like the type in a `compare` `<type>:<id>` ref, is letters and hyphens (`item`, `item-set`,
`battle-pet`) and `ID` is at least 1; anything else is `invalid_argument`, exit 2, before any request. `entity` and `entity-page` take `TYPE ID` or
`--url`, and passing both is `invalid_argument`. Item sets (tier sets such as "Battlegear of Wrath")
come back from `search` and `resolve` as `entity_type: "item-set"` with `wowhead entity item-set <id>`
as the follow-up, and `resolve --entity-type item-set` keeps only them.

Guides:

| Command | Purpose |
|---------|---------|
| `guides CATEGORY` | guide listing for a category with author, patch, and updated-window filters; a category Wowhead does not have (it serves its whole guide index instead) is `not_found` |
| `guide REF` | one guide: analysis surfaces, linked entities, comments, and page metadata; section bodies come from `guide-full`. `REF` is a guide id, or a Wowhead URL or path whose path, after any expansion and locale prefix (`/de/guide/...`), starts `guide/` or `guide=<id>`; any other page (the home page, `/items`, `/item=19019`, a `/guides/<category>` listing, which `guides` reads) is `invalid_argument`, exit 2. An unknown guide id (Wowhead answers HTTP 400) is `not_found`, exit 4 |
| `guide-full REF` | the same guide with every section, comment, and link hydrated |
| `guide-export REF` | write a guide bundle (manifest, sections, entities) to `--out`, or `./wowhead_exports/guide-<id>-<slug>/`; the root `index.json` next to the bundle is written only when it is absent or already a bundle index; an export that hydrates nothing removes an earlier `entities/manifest.json`; a linked entity that cannot be hydrated is listed in `hydration.failed` (`entity_type`, `id`, `code`, `message`) instead of failing the export |
| `guide-query BUNDLE QUERY` | query one guide bundle for matching sections, links, and comments; answers with the `icy-veins`/`method` guide-query payload (`count`, `match_counts`, `matches`, `top`, `failed_pages`) plus `bundle`, `guide`, and `page`. A blank `QUERY` is `invalid_query`, exit 2, here and in `guide-bundle-search` and `guide-bundle-query`. A bundle path that does not exist is `not_found` (exit 4) here and in `guide-bundle-inspect`/`guide-bundle-refresh`; one that exists but is not a readable bundle is `invalid_bundle` (exit 1). `BUNDLE` may instead name a bundle under `--root`: a guide id or whole directory name or title first, then part of a title or directory name. A name no bundle matches (or a root with none) is `not_found`, and one that matches several is `invalid_argument` (exit 2) listing them |
| `guide-bundle-list` | local bundles with freshness and hydration summaries; a sibling directory whose `manifest.json` cannot be read or decoded is skipped |
| `guide-bundle-search QUERY` | find local bundles by title, id, or directory name |
| `guide-bundle-query QUERY` | rank matches across every local bundle, scored by the same engine as `guide-query` |
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
| `news-post REF` | one news article with body markup, related posts, and citations; a URL must be a `/news/...` or `/news=<id>` page (`invalid_ref` otherwise), and a page with no article body is `parse_failed` |
| `blue-tracker [QUERY]` | blue-post listing with topic, date-window, author, region, and forum filters (`--region` takes `us`, `na` read as `us`, or `eu`, the regions Wowhead files blue posts under; any other value is `invalid_argument`); rows mix `/blue-tracker/topic/...` and `/blue-tracker/news/...` shapes, and only topic rows feed `blue-topic` |
| `blue-topic REF` | one blue-tracker topic with posts, participants, and citations; a URL other than `/blue-tracker/topic/...` is `invalid_ref` |

Tool-state decoders:

| Command | Purpose |
|---------|---------|
| `talent-calc REF` | class, spec (`tool.spec_id` is its Blizzard spec id), and build code from a talent calculator ref; for a classic calculator the build is decoded into `talents` (see [Classic talent builds](#classic-talent-builds)). A classic-era `/classic/talent-calc/<class>/<build-code>` ref has no spec, so `spec_slug` and `spec_id` are null. An unknown class is `invalid_tool_ref`, as is a build code with a character outside letters, digits, `+`, `-` and `_`; so, on a retail, PTR or beta ref, is a spec of another class (`paladin/frost`) or a build code whose loadout header names another spec. A classic calculator's spec is not checked (MoP Classic's rogue `combat` is valid) and its `spec_id` is null. `listed_builds` holds only the ref's spec's builds (the page embeds every spec's), and is absent for a classic calculator ref and for a ref with no spec, since both have a null `spec_id` |
| `talent-calc-packet REF` | exact talent transport packet from a `<class>/<spec>/<build-code>` ref; `--out PATH` writes just the packet. The packet comes from the build code in `REF`, so a failed page fetch still answers, with `page.canonical_url` null and `page.fetch_error` `{code, message}` |
| `profession-tree REF` | profession slug and loadout code |
| `dressing-room REF` | normalized share hash and cited state URL |
| `profiler REF` | normalized `list=` ref with list, region, realm, and name parts; takes Wowhead's canonical `/list=<id>/<slug>` and `/list=<id>/<region>/<realm>/<name>` URLs as well as `/list?list=...` |

`dressing-room` and `profiler` are state inspectors: they normalize and cite the ref, they do not
decode the opaque client-side payload behind it. `profiler` fetches the list page for the ref and
fails with `not_found` (exit 4) when Wowhead answers with its "This list doesn't exist or has been
removed" page. For both, `page.canonical_url` is the fetched page's own canonical link; when the
page names none it is null and `page.note` says so. A share URL under an expansion path
(`/classic/dressing-room#...`) is read from that expansion unless `--expansion` is passed.

### Classic talent builds

`talent-calc` reads every Wowhead calculator: retail (and `/ptr/`, `/beta/`), `/classic/` (Classic
Era and Season of Discovery), `/classic-ptr/`, `/tbc/`, `/wotlk/`, `/cata/`, `/mop-classic/` and
WoW Forever's `/forever/`. A classic path is `<class>/<build-code>`, optionally followed by the
talent selection order Wowhead appends once talents are clicked; a MoP Classic path is
`<class>[/<spec>]/<tiers>[/<glyphs>]`. `tool.extra_segment` holds that trailing segment raw.

For a ref with a build code, `talents` is the decoded build:

| Field | Meaning |
|-------|---------|
| `decoded` | `true` when the build decoded; `false` with a `reason` otherwise |
| `format` | `classic_points` (talent trees) or `mop_tiers` (MoP Classic's one talent per tier) |
| `points_total`, `points_by_tree` | points spent, and per tree in the calculator's order (`17/34/0`) |
| `trees[]` | `tree_id`, `name`, `points`, and `talents[]` with `name`, `rank`, `max_rank`, `spell_id` (the taken rank's spell), `talent_id`, `row`, `col` |
| `tiers[]` | MoP Classic: `tier`, `level`, `choice` (0 for none, else column 1-3) and `talent` `{name, spell_id}` |
| `player_level`, `race_id` | the calculator's level and race suffixes, when the code carries them |
| `glyphs_or_runes_code`, `glyphs_code`, `selection_order` | glyph, rune and click-order segments, returned raw and not decoded |
| `data_url` | the versioned talent data file the calculator loads; its trimmed copy is cached for 30 days (`talent_calc_data` namespace) |

`decoded` is `false`, with the reason, for a retail build (decode it with `warcraft talent-describe`
or `simc describe-build`), for a classic path that names a spec, and for a code the calculator data
cannot account for: a rank above the talent's maximum, more digits than a tree has talents, more
trees than the class has, or a WoW Forever code without its `v2` prefix. Wowhead's calculator
quietly drops such parts, so the decoder refuses rather than guess. Codes in the calculator's older
packed form (starting with a capital letter) decode for the calculators listed in the table below.

Decoding reports the ranks encoded in the URL. It does not validate talent prerequisites, point
budgets or level/race eligibility, so an invalid allocation may differ from Wowhead's rendered
selection. A truncated packed tree or incomplete packed rank returns `decoded: false`.

Each calculator was checked build by build against what Wowhead's own calculator renders (tree
totals, talents, ranks and names), with builds from Wowhead's class guides:

| Calculator | Builds checked | Packed codes |
|------------|----------------|--------------|
| `classic` (Classic Era, Season of Discovery with runes) | 31 | decoded (6 checked) |
| `tbc` | 25 | decoded (2 checked) |
| `wotlk` | 20 | decoded (2 checked) |
| `cata` | 13 | not decoded |
| `mop-classic` | 7 | n/a |
| `forever` | 7: warrior, paladin, hunter, rogue, priest, shaman and mage; made in its calculator (it has no guide builds yet) | not decoded |
| `classic-ptr` | 1 | not decoded |

Forever warlock and druid builds were not checked because Wowhead blocked further calculator
requests. They use the same decoder and class tree-order tables, but have no rendered comparison.

Cache maintenance:

| Command | Purpose |
|---------|---------|
| `cache-inspect` | backend configuration and per-namespace entry counts; `--summary` lists the largest namespaces first, each with its `total` (a Redis namespace reports only that key count) |
| `cache-clear` | clear cached responses for chosen namespaces or all of them; an unknown `--namespace` is a usage error, `--namespace legacy_unscoped` removes file-cache entries from before cache namespacing, and an unreachable Redis is `network_error` (exit 5) |

Run `wowhead <command> --help` for the full flag list of any command.

## Workflows

Resolve, then fetch:

```bash
wowhead resolve "thunderfury"
wowhead entity item 19019
```

Export a guide once, query it repeatedly offline:

```bash
wowhead guide-export 3143 --out ./tmp/guides/guide-3143
wowhead guide-query ./tmp/guides/guide-3143 "talent build"
```

Scan a topic across a date window:

```bash
wowhead news "class tuning" --date-from 2026-09-01 --pages 40
```

Check that `scan.stop_reason` is `date_from_reached`; null means `--pages` ran out before the
window's start, so raise `--pages` (or start from a later `--page`).

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

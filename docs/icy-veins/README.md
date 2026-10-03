# Icy Veins CLI

`icy-veins` is a WoW guide/article provider CLI. It discovers guides from the Icy Veins WoW
sitemap, the site-wide guide menu and a site index that `icy-veins index-refresh` builds by crawling,
fetches and parses guide pages, and exports multi-page guide bundles that can be queried offline.

Tier: **supported**. No auth, no API key.

## Commands

| Command | What it does |
| --- | --- |
| `icy-veins doctor` | Reports capabilities and the resolved HTTP cache configuration. |
| `icy-veins search <query>` | Ranks sitemap, site-menu and site-index guides against a free-text query. |
| `icy-veins resolve <query>` | Picks the single best guide and returns a `next_command`. |
| `icy-veins guide <guide_ref>` | Fetches one guide page and returns a summary with previews. |
| `icy-veins guide-full <guide_ref>` | Fetches every page in the guide's family navigation and merges them. |
| `icy-veins guide-export <guide_ref>` | Writes the full bundle (pages, entities, analysis surfaces) to a directory. |
| `icy-veins guide-query <bundle> <query>` | Searches an exported bundle without touching the network. |
| `icy-veins index-refresh` | Crawls the site for pages the frozen sitemap lacks and merges them into the local site index. |

`guide_ref` accepts a slug (`mistweaver-monk-pve-healing-guide`) or a full
`https://www.icy-veins.com/wow/<slug>` URL.

## Flags

Global flags come before the subcommand and are the shared agent-output flags:
`--pretty`, `--compact`, `--compact-max-chars`, `--fields`, `--fields-strict`, `--profile`.

Per-command flags:

- `search` / `resolve`: `--limit` (1-50, default 5)
- `guide-export`: `--out <dir>` (defaults to `./icy-veins_exports/guide-<slug>`). An `--out` that
  names an existing file fails with `invalid_argument` (exit 2) before anything is fetched. Exporting
  again into the same directory replaces the bundle's `pages/*.html`, so `pages/` holds only the pages
  `page-files.json` lists; other files in the directory are left alone.
- `guide-query`: `--limit` (1-50, default 5), `--kind` (repeatable or comma-separated), `--section-title`
- `index-refresh`: `--max-requests` (1-5000, default 250), the most uncached page requests the run makes

`--kind` accepts `sections`, `navigation`, `linked_entities`, `build_references`, and
`analysis_surfaces`; all five are searched when the flag is omitted. Anything else fails with
`invalid_argument` (exit 2).

## Examples

```bash
icy-veins --pretty search "mistweaver monk guide" --limit 5
icy-veins resolve "fury warrior easy mode"
icy-veins guide mistweaver-monk-pve-healing-guide
icy-veins guide-export mistweaver-monk-pve-healing-guide --out ./tmp/mw-monk
icy-veins guide-query ./tmp/mw-monk "stat priority" --kind analysis_surfaces
icy-veins index-refresh
icy-veins search voidspire
```

## Output

Every command emits the shared envelope (`ok`, `provider`, `command`, `kind`, `schema_version`,
`query`, `provenance`, `data`, and `error` on failure) and no other top-level key; the payload is in
`data`.

Exit codes follow `docs/foundation/ERROR_CONTRACT.md`: 1 generic, 2 usage, 4 guide not found,
5 network/upstream failure. A blank `search` or `resolve` query fails with `invalid_query` (exit 2)
before any request. A page whose article container no longer matches (an Icy Veins layout change),
or whose canonical link is not a guide page, fails with `parse_failed` and exit 1 rather than
returning an empty article with `ok:true`. So does a sitemap that lists no guide pages (a challenge
page or a reshaped sitemap); that body is not cached. `guide-full` and `guide-export` of a spec guide
or one of its sub-pages (every family from `spec_guide` to `simulations` in the table below) fail with
`parse_failed` when the page's switcher does not parse, rather than returning a one-page bundle.

`doctor` reports `cache.redis_url` without its credentials or query string
(`redis://***@host:6379/0`).

`guide-query` answers with kind `guide_query`: the shared match payload plus `bundle` (the path
queried) and `guide` (the exported guide row), the same shape as `method guide-query`.

`guide-query` answers a bad bundle path the same way `method guide-query` does, echoing the query: a
path that does not exist is `not_found` (exit 4), a file is `invalid_argument` (exit 2), and a directory that is not a
readable bundle is `invalid_bundle` (exit 1): no `manifest.json`, a manifest whose `files` lists no
content file (`pages.jsonl`, `sections.jsonl`, `analysis-surfaces.jsonl`, ...), or a listed file that
is missing, corrupt, or holds a row with a wrongly typed nested field (a `build_identity` that is not
an object, `surface_tags` that is not a list). A `wowhead guide-export` bundle is readable; it has sections, navigation,
linked entities and analysis surfaces, but no pages or build references.

### Build references

`build_references` carries explicit build evidence from the page, never a guess from the slug or
title. Two reference types are emitted:

| `reference_type` | Source on the page | `url` |
| --- | --- | --- |
| `wowhead_talent_calc_url` | an embedded Wowhead talent-calc link | the talent-calc URL |
| `wow_talent_export` | a published WoW loadout import string (the `Copy` blocks on the talents pages) | the import string itself, because the reference has no link |

Both types set `build_code`, so `warcraft guide-builds-simc` collects either one. It counts the
build in `summary.identify_success_count` only when simc identifies one class and spec; otherwise the
identify leg fails `build_not_identified`. Decoding needs a class and a spec, and the two types supply
them differently:

- `wowhead_talent_calc_url` always decodes unaided: its URL path names the class and spec.
- `wow_talent_export` names neither, so `build_identity` stays unknown on the row and SimC has to
  identify the string itself. It probes every spec in SimC's specialization data, healers included.

So `simc decode-build --talents <build_code>` returns `ok:true` for an Icy Veins build of any role
without `--actor-class` or `--spec` (verified against the Fury Warrior talents page: the probe returns
`warrior`/`fury` with `confidence: high`).

The Icy Veins builds/talents pages publish import strings rather than talent-calc links, so in
practice the rows you get back are `wow_talent_export`.

### Partial guide bundles

`guide-full` and `guide-export` walk every page in the family navigation. A page that cannot be
fetched or parsed is skipped rather than failing the whole bundle, and it is reported in
`data.failed_pages` (`{count, items: [{url, section_slug, error: {code, message}}]}`). Content from
those pages is missing from the merged sections, entities, build references, and analysis surfaces.

When Icy Veins serves another guide than the one requested (it retires a page by redirecting its URL:
`mistweaver-monk-legion-remix-guide` serves the healing guide since 2026-09-29), `guide`, `guide-full`
and `guide-export` set `data.redirect` to `{requested, served, message}`, and everything else in the
payload describes the served guide. It is `null` when the requested guide was served.

## Supported guide families

Sitemap discovery and `guide` only accept slugs that classify into a known family. Unclassified WoW
pages fail with `invalid_guide_ref` (exit 2). `guide-full` and `guide-export` still include every
page the guide's own navigation links, so a bundle can carry a page with `content_family: null`
(the Frost Mage switcher links `frost-mage-cosmetics`); read it from the bundle, because
`icy-veins guide` refuses its slug.

| Family | Example slug |
| --- | --- |
| `class_hub` | `monk-guide` |
| `role_guide` | `healing-guide` |
| `spec_guide` | `mistweaver-monk-pve-healing-guide` (only `<spec>-<class>-pve-<role>-guide`) |
| `easy_mode` | `fury-warrior-pve-dps-easy-mode` |
| `leveling` | `mistweaver-monk-leveling-guide` |
| `pvp` | `mistweaver-monk-pvp-guide` |
| `spec_builds_talents` | `mistweaver-monk-pve-healing-spec-builds-talents` |
| `rotation_guide` | `mistweaver-monk-pve-healing-rotation-cooldowns-abilities` |
| `stat_priority` | `mistweaver-monk-pve-healing-stat-priority` |
| `gems_enchants_consumables` | `mistweaver-monk-pve-healing-gems-enchants-consumables` |
| `gear_best_in_slot` | `mistweaver-monk-pve-healing-gear-best-in-slot` |
| `spell_summary` | `mistweaver-monk-pve-healing-spell-summary` |
| `resources` | `mistweaver-monk-resources` |
| `mythic_plus_tips` | `mistweaver-monk-pve-healing-mythic-plus-tips` |
| `macros_addons` | `mistweaver-monk-pve-healing-macros-addons` |
| `simulations` | `mistweaver-monk-pve-healing-simulations` |
| `pvp` | also a spec's PvP sub-pages: `mistweaver-monk-pvp-talents-and-builds` |
| `raid_guide` | `mistweaver-monk-pve-healing-nerub-ar-palace-raid-guide`, `venomous-abyss-raid-guide`, `firelands-raid` |
| `raid_encounter` | boss pages: `drest-agath-normal-encounter-journal`, `al-akir-healer-strategy`, `broodtwister-ovi-nax-raid-guide-in-nerub-ar-palace` |
| `dungeon_guide` | `ara-kara-city-of-echoes-dungeon-guide`, `dungeons-guide` |
| `delve_guide` | `the-sinkhole-delve-guide`, `brann-bronzebeard-delve-companion-guide`, `delves-guide` |
| `profession` | `professions`, `professions-alchemy`, `professions-mining-leveling` |
| `tier_list` | `mythic-dps-tier-list`, `pvp-dps-tier-list`, `tier-lists` |
| `hub` | `void-assaults-hub`, `midnight-mounts-hub`, `guides-for-legion` |
| `expansion_guide` | `mistweaver-monk-the-war-within-pve-guide`, `midnight-expansion-guide` |
| `special_event_guide` | `mistweaver-monk-mists-of-pandaria-remix-guide` |
| `article_guide` | any other `-guide`/`-guides` page (`season-3-mythic-plus-guide`, `frost-mage-hero-talents-pve-guide`) and `weekly-to-do-list` |
| `transmog` | transmog set and item-model pages: `transmogrification-mage-cloth-chest-item-model-list` (the transmog hubs ending in `-guide`/`-guides` are `article_guide`) |

The current raid's boss pages are slugged like the raid's own guide (`vorasius-raid-guide`), so they
classify as `raid_guide`; `search` labels such a page `raid_encounter` when the site index records a
raid guide as its breadcrumb parent, so boss pages do not take the raid-guide boost. `icy-veins guide`
classifies by slug alone and reports them as `raid_guide`.

A `transmog` page loses 30 points (`penalty_transmog`) unless the query says `transmog` or
`transmogrification`, so the 650 set and model pages, which name a class, never crowd out a class or
spec guide. Change analyses (`arcane-mage-patch-9-1-changes-analysis`, `latest-mage-class-changes`),
tool pages (`midnight-talent-calculator`) and news stay unclassified.

A query word naming a dungeon or delve (`dungeon`, `delves`) boosts `dungeon_guide` or `delve_guide`
pages by 18, like `raid` does for `raid_guide`.

`guide-full` traversal is family-aware: class hubs and role guides stay on the current page, and
every other family walks its own navigation block. Only a class hub reads the class dropdown in the
page header as its navigation. A spec page whose own switcher is missing fails as described under
Output; a page of another family without navigation is walked as that one page.

Patch notes, class-change roundups, hotfix posts, and news pages are out of scope. `search` and
`resolve` detect those query intents and return an empty result set with a `scope_hint` instead of
misleading guide matches.

`search` and `resolve` return a guide only when its name or slug contains the whole query or every
query word, or when a query word names the guide's family (`talents`, `stats`, `easy mode`, ...). A
family word ranks a guide only when the guide also matches one of the query's other words (role words
such as `dps` aside), so `mythic+ tier list` lists the tier lists rather than every spec's Mythic+
tips page, and `alchemy leveling` the alchemy page rather than every leveling guide. One
word that no guide contains therefore empties the result. Words match whole, so `dh` does not match
"headhunters", and a trailing plural `s` or `es` is ignored on both sides, so `build` keeps the
`...-spec-builds-talents` pages and `boss` the "world bosses" guide. Words such as `a`, `of` and `the` are ignored. Every Mythic+ spelling
(`m+`, `m plus`, `mythic+`) reads as `mythic plus`, in the query and in page titles alike, so it finds
the "Mythic Plus" pages, the "Mythic+ ... Tier List" pages and the seasonal
`<expansion>-mythic-season-<n>-guide` pages; any other `+` reads as `plus`. Punctuation is folded the way slugs fold it: any
separator other than an apostrophe is a space, and the query is tried with each apostrophe dropped
and as a space (`K'aresh` is `karesh`, `Zul'Aman` is `zul-aman`). A hyphenated word is also tried
with its hyphen dropped, so `Nerub-ar Palace` resolves to `nerubar-palace-raid-guide`
and `Ara-Kara, City of Echoes` to `ara-kara-city-of-echoes-dungeon-guide`. Class and spec shorthand is spelled out in the query and
in page titles alike (`ret pally` is `retribution paladin`, `frost dk` is `frost death knight`, `mw`
is `mistweaver`), so `disc belt` still finds the "Disc Belt Guide".

A spec query (`frost mage`, `survival hunter guide`) resolves to that spec's
`...-pve-<role>-guide`. Healer specs also publish a PvE DPS guide; their healing guide ranks first.
A hunter spec's pets page ranks with its PvP, leveling, hero talents and Mythic+ (`-pve-<role>-mythic-plus-guide`) pages, below the spec guide. A query that
names a spec ranks that spec's guides (`spec_name` in `ranking.match_reasons`) above pages that only
share the word, so `shadow` lists the Shadow Priest guide before the Shadow Enclave delve guide.
`resolve` judges confidence on every ranked match, and `--limit` only trims the `candidates` shown, so
`--limit 1` never makes an ambiguous query look resolved. It never picks between candidates with the
same or nearly the same score, so a spec name that
several classes share (`frost`, `holy`, `protection`, `restoration`) stays unresolved; add the class.
An unresolved `resolve` reports `confidence: "low"` when its top candidates tie on score and
`"medium"` otherwise. `search` and `resolve` report `count` as the rows returned, `total_matches` as
every match, and `truncated: true` when `--limit` cut the list. Every row carries `provider`,
`kind: "guide"`, `id` (the slug), `name`, `url`, `ranking` and `follow_up` (`command`, `surface`).
The one exception is a query that is a page's exact title (`exact_title`) when every close rival is
one of that page's own sub-pages, by slug prefix or by the breadcrumb parent the site index records:
`player housing` resolves to `player-housing-guide` over `player-housing-interior-guide` and
`housing-decor-guide`.

Only a spec, class or role introduction (`spec_guide`, `class_hub`, `role_guide`) gets the
`intro_guide` boost; a season hub or any other `article_guide` does not outrank the pages about what
the query names.

Each result carries `metadata.sitemap_lastmod`, the sitemap's `<lastmod>` date, which is not the
page's own update date (`icy-veins guide` reports that as `guide.last_updated`). A page whose
`sitemap_lastmod` is more than a year before the newest one in the sitemap loses 10 points and lists
`penalty_stale_page` in `ranking.match_reasons`, so a past season's guide ranks below the current one.

`search` and `resolve` put `sitemap_url` and `sitemap_newest_lastmod` in `provenance`. When the newest
entry is more than 30 days old, the sitemap has stopped being updated: as of 2026-10-02 its newest
entry is 2025-10-05 and Icy Veins publishes no other sitemap (`robots.txt` lists only `/sitemap.xml`;
`/sitemap-index.xml`, `/sitemap_index.xml` and `/wow/sitemap.xml` are 404), and the `/wow/` hub
answers scripted clients with a Cloudflare 403.

A stale sitemap makes `search` and `resolve` also read the site-wide guide menu (`nav.iv-subnav`)
that every guide page carries, from one class hub (`provenance.site_menu_url`, cached like any
guide page), and the site index (see [Site index](#site-index)). The menu links the current season's
pages (raid, Mythic+, PvP and season hubs, new specs); the index lists every page an `index-refresh`
crawl has found, past seasons included (the Season 1 raid and its bosses, dungeons, delves, renamed
pages). Search reads the index file only and never crawls. Every result says where it came from in
`metadata.source`, first match wins: `sitemap`, then `site_menu`, then `site_index`.

- A `site_menu` row has `sitemap_lastmod: null`, `name` built from its slug like a sitemap row and
  `metadata.menu_title` with the menu's own wording (`Mythic+ Season 2`), which it also matches on.
- A `site_index` row is named by the page's headline (`Vorasius Raid Guide in The Voidspire for
  Midnight Season 1`) and has `sitemap_lastmod: null`.
- Any row the index knows, whatever its source, also matches on the index headline, so `voidspire`
  finds that raid's boss pages, and carries `metadata.date_published` (the page's JSON-LD
  `datePublished`) and `metadata.parent` (the slug of its breadcrumb parent). Both are `null` for a
  page the index does not hold.
- A renamed page's old slug (a 301 the crawl recorded) is dropped when its new slug is listed, so the
  page is ranked once.

Neither menu nor index rows take the stale penalty. Score ties go to the newest page: a `site_menu`
page first, then by `sitemap_lastmod` or, for a `site_index` page, `date_published`.
`date_modified` is not used: Icy Veins re-saved most pages on 2026-05-19, so it dates nothing.

`provenance.sitemap_warning` then says where pages published since the sitemap's date come from.
When the index in use is the snapshot bundled with the release, or there is none, the warning says
so and suggests `icy-veins index-refresh`. `provenance.site_index_path` and
`provenance.site_index_refreshed_at` name the index read. `provenance.site_index_warning` is set when
your local index was refreshed more than 7 days ago, or when the live menu links pages the index
lacks (it names them); run `icy-veins index-refresh`.

When the menu cannot be read (a block page, or markup that no longer lists any guide), search still
answers from the sitemap and the index, with `provenance.site_menu_warning` saying why and
`provenance.sitemap_warning` saying that newer guides are missing unless the index lists them.

## Site index

`icy-veins index-refresh [--max-requests N]` crawls icy-veins.com for the pages its sitemap lacks and
merges what it reads into a JSON index at `<data root>/icy-veins/site_index.json` (durable data, not
the TTL cache; `<data root>` is `$XDG_DATA_HOME/warcraft`, `~/.local/share/warcraft` by default, and
the checkout's `.warcraft/runtime/data` when run from a worktree).

The crawl:

- starts at `death-knight-guide`, follows every link in the site-wide menu, and follows any link on a
  fetched page to a `/wow/<slug>` page the sitemap does not list, recursively; a page the sitemap
  lists is not fetched, and neither is a page the previous index holds, so a capped run spends its
  requests on pages it has never seen. It then re-reads the previous index's pages, least recently
  seen first; a link found on a re-read page is followed before the next re-read.
- waits at least 1 second between requests (longer when `WARCRAFT_HTTP_MIN_INTERVAL_SECONDS` asks) and
  never retries. A guide page cached by an earlier `guide` call or crawl is reused and costs no
  request, and every page the crawl reads is cached for `ICY_VEINS_PAGE_CACHE_TTL_SECONDS`.
- stops at `--max-requests` uncached requests (default 250; a full run costs one request per indexed
  page plus one per new page, 243 over the bundled snapshot, about 4 minutes). The run is then `partial` with `stop_reason: "max_requests"`, and the
  pages it found but did not fetch are kept as the index's `frontier`, which the next run fetches
  first. The re-reads it did not reach wait for the next run, which starts with them because they
  are the least recently seen.
- stops at the first 403, 429 or Cloudflare challenge (`cf-mitigated: challenge`) with
  `stop_reason: "blocked"` and `data.blocked: {url, status, challenge}`, and does not retry around it.
- stops after 3 server errors (5xx) or transport failures in a row with `stop_reason: "unavailable"`,
  keeping the newly found pages among them in the `frontier`; other failures are listed in
  `data.errors` and the crawl goes on.
- drops a page that answers 404 from the index and records a 301 as a redirect row naming the new
  slug.

Every run merges into the previous index and never replaces it: a page the crawl did not reach again
keeps its row, so past-season pages stay findable after current pages stop linking them. What a
partial, blocked or unavailable run read is merged too, but a blocked or unavailable run keeps the
index's previous `refreshed_at` (so its age warning stands), and a run that read nothing writes
nothing. A seed page that lists no links (a layout change) fails as
`parse_failed` (exit 1) and leaves the index untouched; an unreachable seed fails as `network_error`
(exit 5).

Each row holds `slug`, `url`, `title` (the JSON-LD headline), `date_published`, `date_modified`,
`parent` (breadcrumb parent slug), `source` (how the crawl first found it: `seed`, `menu`, `page` or
`revisit`), `first_seen`, `last_seen`, `status` (`ok` or `redirect`) and `redirect_to`. No HTML is
stored. The file is written atomically.

`data` reports `index_path`, `partial`, `stop_reason`, `counts` (`fetched` requests, `cached` pages,
`pages` read, `new` pages, `aliases`, `dropped` 404s, `errors`, `frontier`, `total` rows),
`new_pages`, `blocked`, `errors` and `previous_index` (the index merged into). Run it weekly and at a
patch or season start.

### Bundled snapshot

The package ships `icy_veins_cli/data/site_index.json`, an index built by a full live run on
2026-10-03 (243 pages, 147 of them missing from the sitemap). Search and `index-refresh` use it
until you have a local index, so every user finds the current season's pages without crawling, and
your first `index-refresh` merges into it. `icy-veins doctor` reports the index in use under
`site_index`. To regenerate the snapshot from a checkout, run `make icy-veins-snapshot`: a live
`index-refresh --max-requests 400` into a temporary data root that replaces the snapshot only when
the run was complete, and fails with its `stop_reason` otherwise. The run starts from the current
snapshot, so pages it holds are kept. Update the page counts above by hand afterwards.

## Caching

HTTP responses are cached through `warcraft_api.cache`. Sitemap XML and guide HTML are cached
separately.

| Variable | Default |
| --- | --- |
| `ICY_VEINS_CACHE_BACKEND` | `file` (`redis` also supported) |
| `ICY_VEINS_CACHE_DIR` | `<cache root>/icy-veins/http` |
| `ICY_VEINS_REDIS_URL` | unset |
| `ICY_VEINS_REDIS_PREFIX` | `icy_veins_cli` |
| `ICY_VEINS_SITEMAP_CACHE_TTL_SECONDS` | `86400` |
| `ICY_VEINS_PAGE_CACHE_TTL_SECONDS` | `3600` |

`icy-veins doctor` prints the resolved values. The site index is not cached data; see
[Site index](#site-index).

## Tests

- `tests/test_icy_veins_cli.py` - parsing, ranking, command contracts, transport error envelopes
- `tests/test_icy_veins_recorded_fixtures.py` - captured real pages (pre-redesign and Astro layouts,
  and a class hub carrying the site-wide guide menu) plus the slug-to-family classification table
- `tests/test_icy_veins_site_index.py` - index page reading over captured raid, dungeon and boss pages,
  the new families, `index-refresh` (policy, cap, block, merge, failures) and search over a synthetic index
- `tests/test_warcraft_content_site_crawler.py` - the pure crawler over synthetic URL-to-HTML maps
- `tests/e2e/test_icy_veins.py` - live end-to-end journeys, run with `make test-e2e E2E_PATHS="tests/e2e/test_icy_veins.py"`

## Not in scope

Login/premium content, non-WoW Icy Veins games, news ingestion, and patch-analysis pages.

## Source links

- `https://www.icy-veins.com/sitemap.xml`
- `https://www.icy-veins.com/wow/death-knight-guide` (site-menu source)
- `https://www.icy-veins.com/wow/monk-guide`
- `https://www.icy-veins.com/wow/healing-guide`
- `https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide`
- [Roadmap](../ROADMAP.md)

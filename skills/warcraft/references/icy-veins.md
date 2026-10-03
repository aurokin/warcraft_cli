# Icy Veins

## Best For

- spec guides and broad guide-family navigation
- class hubs, role guides, and spec subpages
- current raid, boss, dungeon and delve pages, tier lists and profession guides

## Start With

- discovery: `icy-veins search "<query>"`
- conservative match: `icy-veins resolve "<query>"`
- fetch: `icy-veins guide <slug>`
- deeper content: `icy-veins guide-full <slug>`
- local export/query: `icy-veins guide-export ...`, `icy-veins guide-query ...`
- refresh the site index: `icy-veins index-refresh` (about 4 minutes, one request a second)

## Effective Use

- use Icy Veins when the caller needs structured guide families rather than a single direct page
- for broad class or role queries, let `resolve` pick the hub first
- a spec query such as `frost mage` or `survival hunter` resolves to that spec's PvE guide; healer specs resolve to their healing guide; a spec name several classes share (`frost`, `holy`) stays unresolved, so name the class; a query that is a page's exact title (`player housing`) resolves to that page when its only close rivals are its own sub-pages
- every result carries `metadata.sitemap_lastmod`, the sitemap's date for the page (not the page's own `guide.last_updated`); a page a year behind the newest one is marked `penalty_stale_page` and ranked lower
- `resolve --limit` only trims the candidates shown; confidence is judged on every match
- for narrow subpage questions, search terms like `easy mode`, `rotation`, `stat priority`, or `mythic+ tips` work well
- class and spec shorthand works in queries (`ret pally`, `bm hunter`, `frost dk`)
- official names with punctuation work as typed: `Nerub-ar Palace`, `Ara-Kara, City of Echoes`, `K'aresh`
- page headlines from the site index are matched too, so a raid's name finds its boss pages (`voidspire`) and a boss name finds its page (`vorasius`); boss pages come back as `content_family: "raid_encounter"`
- transmog set and model pages only rank for a query that says `transmog`
- `build_references` holds explicit build evidence from the page: embedded Wowhead talent-calc links (`reference_type: wowhead_talent_calc_url`) and published WoW loadout import strings (`reference_type: wow_talent_export`, where `url` is the import string). Guide slugs and titles are never treated as build evidence
- current spec builds/talents pages publish import strings, so `guide-full` on a spec guide is what feeds `guide-builds-simc`
- `wowhead_talent_calc_url` rows always decode unaided, because the URL path names the class and spec
- an import string names neither, so SimC identifies it by probing every spec it knows: builds of every role, healers included, decode without `--actor-class` or `--spec`
- additive `analysis_surfaces` highlight comparison-relevant guide topics without replacing raw guide content

## Validated Families

- class hubs
- role guides
- spec guides
- easy mode
- leveling
- PvP
- builds/talents
- rotation
- stat priority
- gems/enchants/consumables
- spell summary
- resources
- macros/addons
- Mythic+ tips
- simulations
- raid guides
- raid encounters (boss pages)
- dungeons
- delves
- professions
- tier lists
- hubs
- expansion guides
- special-event guides
- transmog (only for transmog queries)

## Boundaries

- patch notes, hotfixes, and news-like queries return `scope_hint` rather than guide results
- a page whose article container no longer matches fails with `parse_failed`; an empty article is never reported as success. So do a sitemap that lists no guides and a spec page whose page switcher no longer parses in `guide-full`/`guide-export`
- discovery reads the Icy Veins sitemap, which has stopped being updated, so `search` and `resolve` also read the site-wide guide menu and the site index. Pages the sitemap lacks come back with `sitemap_lastmod: null` and `metadata.source` saying where they came from: `site_menu` (current pages the live menu links) or `site_index` (pages a crawl found, past seasons included, named by their headline). Rows the index knows carry `metadata.date_published` and `metadata.parent` (breadcrumb parent slug); prefer `date_published` over the sitemap date to judge how new a page is
- the site index is a local file that `icy-veins index-refresh` builds; until it exists, search uses the snapshot bundled with the release and `provenance.sitemap_warning` says so. Run `icy-veins index-refresh` when `provenance.site_index_warning` is set (the index is over a week old, or the live menu links pages it lacks) or at a patch or season start. It returns `data.partial`: `stop_reason: "max_requests"` means run it again to continue; `stop_reason: "blocked"` means Icy Veins refused the crawl, so wait rather than retry. A partial run still keeps every page the index had
- search never crawls; a page neither the sitemap, the menu nor the index lists is still missing, so open a known page directly with `icy-veins guide <slug-or-url>`. When `provenance.site_menu_warning` is set the menu could not be read and current pages missing from the index are missing too; use Method or Wowhead for them
- `guide-full` and `guide-export` skip a family page they cannot fetch or parse and list it in `data.failed_pages`
- a bundle can include a navigation page with `content_family: null` (the Frost Mage guide links `frost-mage-cosmetics`) that `icy-veins guide` refuses as `invalid_guide_ref`; read it from `guide-full` or the exported bundle
- when Icy Veins serves another guide than the one asked for (a retired page redirects, e.g. `mistweaver-monk-legion-remix-guide` now serves the healing guide), `guide`, `guide-full` and `guide-export` set `data.redirect` to `{requested, served, message}` and everything else describes the served guide; check it before treating the content as the page you asked for. It is `null` otherwise
